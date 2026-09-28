"""The long-running loop: poll, plan, execute, sleep; full reconcile on a slow schedule."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from keepsake.backend import AuthError, Backend, BackendError, RetryLater
from keepsake.config import Config
from keepsake.engine import SyncBlocked, sync_pass
from keepsake.notify import Notifier
from keepsake.planner import describe
from keepsake.state import LAST_ERROR, LAST_ERROR_AT, StateStore

log = logging.getLogger(__name__)

MAX_BACKOFF = 1800.0
AUTH_BACKOFF_START = 60.0
AUTH_BACKOFF_MAX = 3600.0
# Notify when no pass has succeeded for this long for any non-auth reason.
FAILING_NOTIFY_AFTER = 3600.0

# Fixed notification texts: exception messages can embed Apple's response bodies (account and
# session identifiers), and the webhook is a third party. Details stay in journald.
AUTH_MESSAGE = (
    "Sync stopped: authentication failed. SSH in, check `keepsake status`, and re-run "
    "`keepsake auth keep` or `keepsake auth icloud`."
)
FAILING_MESSAGE = "No successful sync for over an hour. SSH in and check `keepsake status`."

BackendFactory = Callable[[], Backend]


def record_error(store: StateStore, message: str) -> None:
    store.set(LAST_ERROR, message)
    store.set(LAST_ERROR_AT, datetime.now(UTC).isoformat(timespec="seconds"))


class Daemon:
    def __init__(
        self,
        config: Config,
        store: StateStore,
        notifier: Notifier,
        keep_factory: BackendFactory,
        reminders_factory: BackendFactory,
        *,
        stop: threading.Event | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.store = store
        self.notifier = notifier
        self.keep_factory = keep_factory
        self.reminders_factory = reminders_factory
        self.stop = stop or threading.Event()
        self.clock = clock
        self.keep: Backend | None = None
        self.reminders: Backend | None = None
        self.last_full: float | None = None
        self.failures = 0
        self.auth_failures = 0
        self.last_ok = clock()

    def run(self, max_passes: int | None = None) -> None:
        passes = 0
        while not self.stop.is_set():
            delay = self.step()
            passes += 1
            if max_passes is not None and passes >= max_passes:
                return
            self.stop.wait(delay)

    def _due_full(self) -> bool:
        return (
            self.last_full is None
            or self.clock() - self.last_full >= self.config.full_reconcile_interval
        )

    def _backoff(self, base: float, cap: float) -> float:
        return min(cap, base * 2 ** max(self.failures - 1, 0))

    def _check_failing(self) -> None:
        if self.clock() - self.last_ok >= FAILING_NOTIFY_AFTER:
            self.notifier.notify("failing", FAILING_MESSAGE)

    def step(self) -> float:
        """Run one pass; return how long to sleep before the next."""
        try:
            if self.keep is None:
                self.keep = self.keep_factory()
            if self.reminders is None:
                self.reminders = self.reminders_factory()
                self.last_full = None  # fresh backend: its cache starts empty
            full = self._due_full()
            result = sync_pass(
                self.keep,
                self.reminders,
                self.store,
                self.config.policy,
                self.config.thresholds,
                full=full,
            )
        except SyncBlocked as blocked:
            log.warning("pass blocked: %s", blocked)
            for op in blocked.ops:
                log.warning("  would: %s", describe(op))
            record_error(self.store, f"blocked: {blocked}")
            self.notifier.notify(blocked.kind, str(blocked))  # counts only, no item text
            return self.config.poll_interval
        except AuthError as exc:
            self.auth_failures += 1
            self.keep = self.reminders = None  # reconnect from saved credentials next time
            delay = min(AUTH_BACKOFF_MAX, AUTH_BACKOFF_START * 2 ** (self.auth_failures - 1))
            log.error("AUTH FAILURE, all writes stopped: %s (retrying in %.0fs)", exc, delay)
            record_error(self.store, f"auth: {exc}")
            self.notifier.notify("auth", AUTH_MESSAGE)
            return delay
        except RetryLater as exc:
            delay = max(exc.retry_after or self.config.poll_interval, 5.0)
            log.warning("service asked us to back off: %s (retrying in %.0fs)", exc, delay)
            self._check_failing()
            return delay
        except BackendError as exc:
            self.failures += 1
            self.last_full = None  # re-read everything after a failed pass
            delay = self._backoff(self.config.poll_interval, MAX_BACKOFF)
            log.error("pass failed: %s (retrying in %.0fs)", exc, delay)
            record_error(self.store, f"error: {exc}")
            self._check_failing()
            return delay
        except Exception as exc:
            self.failures += 1
            self.keep = self.reminders = None
            delay = self._backoff(self.config.poll_interval, MAX_BACKOFF)
            log.exception("unexpected error (retrying in %.0fs)", delay)
            record_error(self.store, f"unexpected: {type(exc).__name__}: {exc}")
            self._check_failing()
            return delay

        self.last_ok = self.clock()
        if full:
            self.last_full = self.last_ok
        if self.failures or self.auth_failures:
            log.info("recovered")
        self.failures = self.auth_failures = 0
        for issue in ("auth", "empty_fetch", "delete_threshold", "failing"):
            self.notifier.resolved(issue)
        if result.ops:
            log.info("pass applied %d operation(s)", len(result.ops))
        return self.config.poll_interval
