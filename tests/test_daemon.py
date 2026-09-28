from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from keepsake.backend import AuthError, Backend, BackendError, FakeBackend, Item, RetryLater
from keepsake.config import load
from keepsake.daemon import Daemon
from keepsake.notify import Notifier
from keepsake.state import LAST_ERROR, StateStore

CONFIG = """
[keep]
email = "a@gmail.com"
note_title = "Groceries"
[reminders]
apple_id = "a@icloud.com"
list_name = "Groceries"
[sync]
poll_interval = 60
full_reconcile_interval = 3600
[notify]
webhook_url = "https://ntfy.example/topic"
"""


class CountingFake(FakeBackend):
    def __init__(self, prefix: str, *, buffered: bool = False) -> None:
        super().__init__(prefix, buffered=buffered)
        self.fetches: list[str] = []

    def snapshot(self) -> list[Item]:
        self.fetches.append("full")
        return super().snapshot()

    def poll(self) -> list[Item]:
        self.fetches.append("poll")
        self._maybe_fail("poll")
        return FakeBackend.snapshot(self)


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(CONFIG)
        self.config = load(path)
        self.store = StateStore(":memory:")
        self.sent: list[tuple[str, str]] = []
        self.now = datetime(2026, 9, 28, tzinfo=UTC)
        self.notifier = Notifier(
            self.config.webhook_url,
            4,
            self.store,
            post=lambda url, title, msg: self.sent.append((title, msg)),
            now=lambda: self.now,
        )
        self.keep = CountingFake("k", buffered=True)
        self.rem = CountingFake("r")
        self.keep_error: Exception | None = None
        self.rem_builds = 0
        self.t = 0.0
        self.daemon = Daemon(
            self.config,
            self.store,
            self.notifier,
            self.make_keep,
            self.make_rem,
            clock=lambda: self.t,
        )

    def make_keep(self) -> Backend:
        if self.keep_error:
            raise self.keep_error
        return self.keep

    def make_rem(self) -> Backend:
        self.rem_builds += 1
        return self.rem

    def step(self, advance: float = 60) -> float:
        delay = self.daemon.step()
        self.t += advance
        return delay


def test_first_pass_is_full_then_polls_then_hourly_full(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.keep.user_add("Milk")
    assert h.step() == 60
    assert h.rem.fetches == ["full"]
    assert [i.text for i in h.rem.items.values()] == ["Milk"]
    for _ in range(3):
        h.step()
    assert h.rem.fetches == ["full", "poll", "poll", "poll"]
    h.t += 3600
    h.step()
    assert h.rem.fetches[-1] == "full"


def test_auth_failure_stops_writes_notifies_once_and_backs_off(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.step()
    h.rem.fail_on["poll"] = h.rem.fail_on["snapshot"] = AuthError("session expired")
    h.keep.user_add("Eggs")
    delays = [h.step() for _ in range(3)]
    assert delays == [60, 120, 240]
    assert h.rem.items == {}  # nothing written while auth is broken
    assert len(h.sent) == 1 and "auth" in h.sent[0][0]
    assert (h.store.get(LAST_ERROR) or "").startswith("auth:")
    h.rem.fail_on.clear()
    h.step()
    assert h.rem_builds == 4  # initial build plus a reconnect after each auth failure
    assert [i.text for i in h.rem.items.values()] == ["Eggs"]
    assert h.rem.fetches[-1] == "full"  # rebuilt backend starts with a full read
    h.rem.fail_on["poll"] = AuthError("again")
    h.step()
    assert len(h.sent) == 2  # resolved, so a new occurrence notifies right away


def test_notifications_are_rate_limited_per_issue(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.notifier.notify("auth", "x")
    h.notifier.notify("auth", "x")
    h.notifier.notify("delete_threshold", "y")
    h.now += timedelta(hours=5)
    h.notifier.notify("auth", "x")
    assert [t for t, _ in h.sent] == [
        "keepsake: auth",
        "keepsake: delete_threshold",
        "keepsake: auth",
    ]


def test_guard_trip_notifies_and_keeps_polling(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    for i in range(4):
        h.keep.user_add(f"item {i}")
    h.step()
    h.keep.return_empty = True
    assert h.step() == 60
    assert len(h.rem.items) == 4
    assert h.sent and "empty_fetch" in h.sent[0][0]


def test_retry_later_honors_retry_after(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.rem.fail_on["snapshot"] = RetryLater("indexing", 31)
    assert h.step() == 31
    assert h.sent == []
    del h.rem.fail_on["snapshot"]
    h.step()
    assert h.rem.fetches[-1] == "full"


def test_backend_errors_back_off_and_force_a_full_read(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.step()
    h.keep.fail_on["snapshot"] = BackendError("network")
    assert [h.step(), h.step()] == [60, 120]
    del h.keep.fail_on["snapshot"]
    assert h.step() == 60
    assert h.rem.fetches[-1] == "full"


def test_run_stops_on_event(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.daemon.stop.set()
    h.daemon.run()
    assert h.rem.fetches == []
