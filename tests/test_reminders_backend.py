"""RemindersBackend against a fake service modelling the behavior seen in the live spikes."""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import pytest
import requests
from pyicloud.exceptions import PyiCloudAPIResponseException
from pyicloud.services.reminders import ListRemindersResult, Reminder, ReminderChangeEvent
from pyicloud.services.reminders.client import RemindersApiError, RemindersAuthError

from keepsake.backend import AuthError, BackendError, ConflictError, NotFoundError, RetryLater
from keepsake.reminders_backend import DECODE_FAILURE_TITLE, RemindersBackend
from keepsake.state import REMINDERS_CURSOR, StateStore

LIST = "List/OURS"
OTHER = "List/OTHER"
T0 = datetime(2026, 9, 28, tzinfo=UTC)


class FakeService:
    """Merged-per-record change feed, soft deletes, change-tag optimistic concurrency."""

    def __init__(self) -> None:
        self.records: dict[str, Reminder] = {}
        self.log: list[tuple[int, str]] = []  # (seq, record id)
        self.tombstones: set[str] = set()
        self.seq = 0
        self.tags = itertools.count(1)
        self.calls: list[str] = []
        self.fail: dict[str, Exception] = {}
        self.during_list_reminders: list[str] = []  # user edits applied mid-read

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise self.fail.pop(name)

    def _touch(self, rem: Reminder) -> None:
        self.seq += 1
        rem.record_change_tag = f"t{next(self.tags)}"
        rem.modified = T0 + timedelta(seconds=self.seq)
        self.records[rem.id] = rem
        self.log.append((self.seq, rem.id))

    # Helpers standing in for the user on iOS.
    def user_add(
        self, title: str, *, list_id: str = LIST, completed: bool = False, parent: str | None = None
    ) -> str:
        rid = f"Reminder/{len(self.records) + len(self.tombstones) + 1}"
        self._touch(
            Reminder(
                id=rid, list_id=list_id, title=title, completed=completed, parent_reminder_id=parent
            )
        )
        return rid

    def user_edit(self, rid: str, **fields: object) -> None:
        rem = self.records[rid].model_copy(deep=True)
        for k, v in fields.items():
            setattr(rem, k, v)
        self._touch(rem)

    def purge(self, rid: str) -> None:
        del self.records[rid]
        self.seq += 1
        self.tombstones.add(rid)
        self.log.append((self.seq, rid))

    # Service protocol.
    def sync_cursor(self) -> str:
        self._check("sync_cursor")
        return str(self.seq)

    def iter_changes(self, *, since: str | None = None) -> Iterable[ReminderChangeEvent]:
        self._check("iter_changes")
        assert since is not None, "never walk the whole zone"
        changed = dict.fromkeys(rid for seq, rid in self.log if seq > int(since))
        for rid in changed:
            if rid in self.tombstones:
                yield ReminderChangeEvent(type="deleted", reminder_id=rid, reminder=None)
                continue
            rem = self.records[rid].model_copy(deep=True)
            yield ReminderChangeEvent(
                type="deleted" if rem.deleted else "updated", reminder_id=rid, reminder=rem
            )

    def list_reminders(
        self, list_id: str, include_completed: bool = False, results_limit: int = 200
    ) -> ListRemindersResult:
        self._check("list_reminders")
        assert include_completed
        snapshot = [r.model_copy(deep=True) for r in self.records.values() if r.list_id == list_id]
        for rid in self.during_list_reminders:
            self.user_edit(rid, title=self.records[rid].title + " (edited mid-read)")
        self.during_list_reminders = []
        return ListRemindersResult(
            reminders=snapshot,
            alarms={},
            triggers={},
            attachments={},
            hashtags={},
            recurrence_rules={},
        )

    def get(self, reminder_id: str) -> Reminder:
        self._check("get")
        if reminder_id not in self.records:
            raise LookupError(f"Reminder not found: {reminder_id}")
        return self.records[reminder_id].model_copy(deep=True)

    def create(self, list_id: str, title: str, desc: str = "", completed: bool = False) -> Reminder:
        self._check("create")
        rid = self.user_add(title, list_id=list_id, completed=completed)
        return self.records[rid].model_copy(deep=True)

    def _write(self, reminder: Reminder) -> None:
        stored = self.records[reminder.id]
        if stored.record_change_tag != reminder.record_change_tag:
            raise RemindersApiError(
                "Update reminder failed",
                payload=[
                    {
                        "recordName": reminder.id,
                        "serverErrorCode": "CONFLICT",
                        "reason": "client oplock error updating record",
                    }
                ],
            )
        self._touch(reminder.model_copy(deep=True))
        reminder.record_change_tag = self.records[reminder.id].record_change_tag

    def update(self, reminder: Reminder) -> None:
        self._check("update")
        if reminder.completed and reminder.completed_date is None:
            reminder.completed_date = T0
        self._write(reminder)

    def delete(self, reminder: Reminder) -> None:
        self._check("delete")
        reminder.deleted = True
        self._write(reminder)


def setup() -> tuple[FakeService, RemindersBackend, StateStore]:
    svc = FakeService()
    store = StateStore(":memory:")
    return svc, RemindersBackend(svc, LIST, store), store


def texts(backend: RemindersBackend) -> dict[str, tuple[str, bool]]:
    return {i.backend_id: (i.text, i.checked) for i in backend.poll()}


def test_snapshot_scopes_to_top_level_live_items_in_our_list() -> None:
    svc, backend, store = setup()
    milk = svc.user_add("Milk")
    svc.user_add("Nails", list_id=OTHER)
    svc.user_add("sub", parent=milk)
    gone = svc.user_add("Gone")
    svc.user_edit(gone, deleted=True)
    done = svc.user_add("Eggs", completed=True)
    items = {i.backend_id: i for i in backend.snapshot()}
    assert set(items) == {milk, done}
    assert items[done].checked
    assert items[milk].modified_at is not None
    assert svc.calls[:2] == ["sync_cursor", "list_reminders"]  # cursor before the read
    assert store.get(REMINDERS_CURSOR) is not None


def test_edits_during_the_full_read_are_replayed_by_the_next_poll() -> None:
    svc, backend, _ = setup()
    milk = svc.user_add("Milk")
    svc.during_list_reminders = [milk]
    backend.snapshot()
    assert texts(backend)[milk] == ("Milk (edited mid-read)", False)


def test_poll_applies_feed_events() -> None:
    svc, backend, _ = setup()
    milk = svc.user_add("Milk")
    eggs = svc.user_add("Eggs")
    bread = svc.user_add("Bread")
    tea = svc.user_add("Tea")
    backend.snapshot()
    svc.user_add("Nails", list_id=OTHER)  # other list
    svc.user_add("sub", parent=milk)  # subtask
    svc.user_edit(milk, completed=True)
    svc.user_edit(eggs, list_id=OTHER)  # moved out of our list
    svc.user_edit(bread, deleted=True)  # soft delete: event still carries the reminder
    svc.purge(tea)  # tombstone: reminder=None
    coffee = svc.user_add("Coffee")
    assert texts(backend) == {milk: ("Milk", True), coffee: ("Coffee", False)}
    assert "list_reminders" not in svc.calls[2:]


def test_own_writes_echo_back_unchanged() -> None:
    svc, backend, _ = setup()
    milk = svc.user_add("Milk")
    backend.snapshot()
    new = backend.create("Eggs", False)
    backend.update(milk, text="Oat milk", checked=True)
    assert texts(backend) == {milk: ("Oat milk", True), new: ("Eggs", False)}
    backend.delete(new)
    assert texts(backend) == {milk: ("Oat milk", True)}
    assert "get" not in svc.calls  # writes use the cached record, no refetch


def test_completing_resets_completion_date() -> None:
    svc, backend, _ = setup()
    milk = svc.user_add("Milk")
    svc.user_edit(milk, completed=True, completed_date=T0 - timedelta(days=30))
    svc.user_edit(milk, completed=False)
    backend.snapshot()
    backend.update(milk, checked=True)
    assert svc.records[milk].completed_date == T0


def test_conflict_raises_and_refreshes_the_cached_record() -> None:
    svc, backend, _ = setup()
    milk = svc.user_add("Milk")
    backend.snapshot()
    svc.user_edit(milk, title="Whole milk")  # user edit the feed hasn't delivered yet
    with pytest.raises(ConflictError):
        backend.update(milk, checked=True)
    assert svc.records[milk].title == "Whole milk"  # not clobbered
    backend.update(milk, checked=True)  # refreshed tag: succeeds now
    assert svc.records[milk].completed and svc.records[milk].title == "Whole milk"


def test_conflict_refresh_failure_keeps_the_stale_copy() -> None:
    svc, backend, _ = setup()
    milk = svc.user_add("Milk")
    backend.snapshot()
    svc.user_edit(milk, title="Whole milk")
    svc.fail["get"] = requests.ConnectionError("down")
    with pytest.raises(ConflictError):
        backend.update(milk, checked=True)
    assert milk in {i.backend_id for i in backend._items()}  # pyright: ignore[reportPrivateUsage]


def test_write_to_unknown_item_is_not_found() -> None:
    _, backend, _ = setup()
    backend.snapshot()
    with pytest.raises(NotFoundError):
        backend.update("Reminder/nope", checked=True)


def _api_exc(status: int, body: str) -> PyiCloudAPIResponseException:
    response = requests.Response()
    response.status_code = status
    response._content = body.encode()  # pyright: ignore[reportPrivateUsage]
    return PyiCloudAPIResponseException("Bad Request", status, response)


@pytest.mark.parametrize(
    ("error", "expected", "retry_after"),
    [
        (
            _api_exc(
                400,
                '{"retryAfter": 31, "serverErrorCode": "TRY_AGAIN_LATER", '
                '"reason": "These index(es) is/are not valid; Indexing scheduled"}',
            ),
            RetryLater,
            31.0,
        ),
        (_api_exc(421, "{}"), AuthError, None),
        (_api_exc(500, "oops"), BackendError, None),
        (RemindersAuthError("401"), AuthError, None),
        (RemindersApiError("rate", payload={"retry_after": 5}), RetryLater, 5.0),
        (requests.Timeout("slow"), BackendError, None),
    ],
)
def test_error_mapping(
    error: Exception, expected: type[Exception], retry_after: float | None
) -> None:
    svc, backend, _ = setup()
    svc.fail["list_reminders"] = error
    with pytest.raises(expected) as info:
        backend.snapshot()
    if retry_after is not None:
        assert isinstance(info.value, RetryLater) and info.value.retry_after == retry_after


def test_feed_error_falls_back_to_full_read_but_auth_error_propagates() -> None:
    svc, backend, _ = setup()
    milk = svc.user_add("Milk")
    backend.snapshot()
    svc.fail["iter_changes"] = RemindersApiError("expired token", payload={"reason": "x"})
    assert milk in texts(backend)
    assert svc.calls.count("list_reminders") == 2
    svc.fail["iter_changes"] = RemindersAuthError("401")
    with pytest.raises(AuthError):
        backend.poll()


def test_undecodable_title_fails_the_read() -> None:
    svc, backend, _ = setup()
    svc.user_add(DECODE_FAILURE_TITLE)
    with pytest.raises(BackendError, match="decode"):
        backend.snapshot()


def test_undecodable_title_in_another_list_is_ignored() -> None:
    svc, backend, _ = setup()
    milk = svc.user_add("Milk")
    backend.snapshot()
    svc.user_add(DECODE_FAILURE_TITLE, list_id=OTHER)
    assert texts(backend) == {milk: ("Milk", False)}
