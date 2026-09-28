"""Apple Reminders backend over pyicloud's CloudKit RemindersService (unofficial; see CLAUDE.md).

Verified against pyicloud 2.7.0 and live spikes:
- ``lists()`` walks the zone's whole change history (~60 s), so it is never called here; the
  list id is resolved once by ``keepsake auth icloud`` and stored.
- Steady state is ``sync_cursor()`` + ``iter_changes(since=cursor)`` (~1.3 s). The feed spans
  every list and includes subtasks; it returns each changed record's latest state. Soft deletes
  arrive as ``type="deleted"`` with ``reminder.deleted=True``; tombstones have ``reminder=None``.
- ``update()``/``delete()`` take a full ``Reminder`` model and write many fields from it
  (due date, priority, ...), guarded by ``record_change_tag``. We write from our cached copy of
  the record, so a concurrent edit makes CloudKit answer CONFLICT instead of being overwritten.
- Our own writes come back through the feed with exactly the written values.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Generator, Iterable
from datetime import UTC, datetime
from typing import Any, Protocol

import pydantic
import requests
from pyicloud.exceptions import (
    PyiCloud2FARequiredException,
    PyiCloud2SARequiredException,
    PyiCloudAPIResponseException,
    PyiCloudAuthRequiredException,
    PyiCloudException,
    PyiCloudFailedLoginException,
)
from pyicloud.services.reminders import ListRemindersResult, Reminder, ReminderChangeEvent
from pyicloud.services.reminders.client import RemindersApiError, RemindersAuthError

from keepsake.backend import AuthError, BackendError, ConflictError, NotFoundError, RetryLater
from keepsake.model import Item
from keepsake.state import REMINDERS_CURSOR, REMINDERS_CURSOR_AT, StateStore

log = logging.getLogger(__name__)

# pyicloud substitutes this when a TitleDocument cannot be decoded. Syncing it would rename the
# item on Keep, so treat it as a failed read.
DECODE_FAILURE_TITLE = "Error Decoding Title"
SLOW_CURSOR_SECONDS = 5.0
AUTH_HTTP_CODES = {401, 403, 421, 450}


class ReminderService(Protocol):
    """The subset of pyicloud's RemindersService we use."""

    def sync_cursor(self) -> str: ...
    def iter_changes(self, *, since: str | None = None) -> Iterable[ReminderChangeEvent]: ...
    def list_reminders(
        self, list_id: str, include_completed: bool = False, results_limit: int = 200
    ) -> ListRemindersResult: ...
    def get(self, reminder_id: str) -> Reminder: ...
    def create(
        self, list_id: str, title: str, desc: str = "", completed: bool = False
    ) -> Reminder: ...
    def update(self, reminder: Reminder) -> None: ...
    def delete(self, reminder: Reminder) -> None: ...


def _server_error(payload: Any) -> tuple[str | None, float | None]:
    """Extract (serverErrorCode, retryAfter) from a CloudKit error payload of any shape."""
    items: list[Any] = payload if isinstance(payload, list) else [payload]
    for entry in items:
        if isinstance(entry, dict):
            code = entry.get("serverErrorCode")
            retry = entry.get("retryAfter", entry.get("retry_after"))
            if code or retry is not None:
                return (str(code) if code else None, float(retry) if retry is not None else None)
    return None, None


def _response_json(exc: PyiCloudAPIResponseException) -> Any:
    if exc.response is None:
        return None
    try:
        return exc.response.json()
    except ValueError:
        return None


@contextlib.contextmanager
def reminders_errors(label: str) -> Generator[None]:
    """Time a pyicloud call and translate its exceptions into backend errors."""
    start = time.monotonic()
    try:
        yield
    except BackendError:
        raise
    except (
        RemindersAuthError,
        PyiCloudAuthRequiredException,
        PyiCloud2FARequiredException,
        PyiCloud2SARequiredException,
        PyiCloudFailedLoginException,
    ) as exc:
        raise AuthError(f"iCloud auth failed: {type(exc).__name__}: {exc}") from exc
    except RemindersApiError as exc:
        code, retry = _server_error(exc.payload)
        if code == "CONFLICT":
            raise ConflictError(f"record changed remotely: {exc}") from exc
        if code == "TRY_AGAIN_LATER" or retry is not None:
            raise RetryLater(f"CloudKit asked to retry later: {exc}", retry) from exc
        raise BackendError(f"CloudKit error: {exc}") from exc
    except PyiCloudAPIResponseException as exc:
        code, retry = _server_error(_response_json(exc))
        if code == "TRY_AGAIN_LATER" or retry is not None:
            raise RetryLater(f"CloudKit asked to retry later ({code})", retry) from exc
        if exc.code in AUTH_HTTP_CODES:
            raise AuthError(f"iCloud auth failed (HTTP {exc.code})") from exc
        raise BackendError(f"iCloud error: {exc}") from exc
    except PyiCloudException as exc:
        raise BackendError(f"iCloud error: {type(exc).__name__}: {exc}") from exc
    except LookupError as exc:
        raise NotFoundError(str(exc)) from exc
    except (requests.RequestException, pydantic.ValidationError) as exc:
        raise BackendError(f"iCloud request failed: {type(exc).__name__}: {exc}") from exc
    finally:
        log.debug("reminders.%s took %.2fs", label, time.monotonic() - start)


class RemindersBackend:
    buffered = False

    def __init__(self, service: ReminderService, list_id: str, store: StateStore | None = None):
        self._svc = service
        self._list_id = list_id
        self._store = store
        self._cache: dict[str, Reminder] = {}
        self._cursor: str | None = None

    # Reads.
    def _ours(self, rem: Reminder) -> bool:
        return rem.list_id == self._list_id and not rem.deleted and not rem.parent_reminder_id

    def _accept(self, rem: Reminder) -> None:
        # The feed spans every list; only our own records can fail the read.
        if not self._ours(rem):
            self._cache.pop(rem.id, None)
            return
        if rem.title == DECODE_FAILURE_TITLE:
            raise BackendError(f"pyicloud could not decode the title of {rem.id}")
        self._cache[rem.id] = rem

    def _take_cursor(self) -> str:
        start = time.monotonic()
        with reminders_errors("sync_cursor"):
            cursor = self._svc.sync_cursor()
        elapsed = time.monotonic() - start
        if elapsed > SLOW_CURSOR_SECONDS:
            log.warning(
                "sync_cursor() took %.1fs; pyicloud may have fallen back to a full zone walk",
                elapsed,
            )
        return cursor

    def _set_cursor(self, cursor: str) -> None:
        self._cursor = cursor
        if self._store is not None:
            self._store.set(REMINDERS_CURSOR, cursor)
            self._store.set(REMINDERS_CURSOR_AT, datetime.now(UTC).isoformat(timespec="seconds"))

    def _items(self) -> list[Item]:
        return [Item(r.id, r.title, r.completed, r.modified) for r in self._cache.values()]

    def snapshot(self) -> list[Item]:
        # Take the cursor first: anything that changes during the read is replayed next poll.
        cursor = self._take_cursor()
        with reminders_errors("list_reminders"):
            result = self._svc.list_reminders(self._list_id, include_completed=True)
        self._cache = {}
        for rem in result.reminders:
            self._accept(rem)
        self._set_cursor(cursor)
        return self._items()

    def poll(self) -> list[Item]:
        if self._cursor is None:
            return self.snapshot()
        cursor = self._take_cursor()
        try:
            with reminders_errors("iter_changes"):
                events = list(self._svc.iter_changes(since=self._cursor))
        except (AuthError, RetryLater):
            raise
        except BackendError as exc:
            log.warning("change feed failed (%s); falling back to a full read", exc)
            return self.snapshot()
        for event in events:
            if event.type == "deleted" or event.reminder is None:
                self._cache.pop(event.reminder_id, None)
            else:
                self._accept(event.reminder)
        log.debug("applied %d change event(s)", len(events))
        self._set_cursor(cursor)
        return self._items()

    # Writes (write-through).
    def _cached(self, backend_id: str) -> Reminder:
        rem = self._cache.get(backend_id)
        if rem is None:
            raise NotFoundError(f"reminder {backend_id} not in list")
        return rem.model_copy(deep=True)

    def _refresh_after_conflict(self, backend_id: str) -> None:
        # The change feed may not have delivered the newer record yet; refetch it so the next
        # pass plans against, and writes with, the current change tag.
        # On any other failure keep the stale copy: dropping it would look like a deletion.
        try:
            with reminders_errors("get"):
                fresh = self._svc.get(backend_id)
            self._accept(fresh)
        except NotFoundError:
            self._cache.pop(backend_id, None)
        except AuthError:
            raise
        except BackendError as exc:
            log.warning("could not refresh %s after conflict: %s", backend_id, exc)

    def create(self, text: str, checked: bool) -> str:
        with reminders_errors("create"):
            rem = self._svc.create(self._list_id, text, completed=checked)
        self._cache[rem.id] = rem
        return rem.id

    def update(
        self, backend_id: str, *, text: str | None = None, checked: bool | None = None
    ) -> None:
        rem = self._cached(backend_id)
        if text is not None:
            rem.title = text
        if checked is not None and rem.completed != checked:
            rem.completed = checked
            rem.completed_date = None  # pyicloud stamps "now" when completing
        try:
            with reminders_errors("update"):
                self._svc.update(rem)
        except ConflictError:
            self._refresh_after_conflict(backend_id)
            raise
        self._cache[backend_id] = rem  # pyicloud refreshed its record_change_tag

    def delete(self, backend_id: str) -> None:
        rem = self._cached(backend_id)
        try:
            with reminders_errors("delete"):
                self._svc.delete(rem)
        except ConflictError:
            self._refresh_after_conflict(backend_id)
            raise
        self._cache.pop(backend_id, None)

    def flush(self) -> None:
        pass

    def discard(self) -> None:
        pass
