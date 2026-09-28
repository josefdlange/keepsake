"""Optional webhook notifications (plain POST, e.g. ntfy.sh), rate-limited per issue."""

from __future__ import annotations

import logging
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from keepsake.state import StateStore

log = logging.getLogger(__name__)

Poster = Callable[[str, str, str], None]


def post_webhook(url: str, title: str, message: str) -> None:
    request = urllib.request.Request(  # noqa: S310 - URL comes from the user's own config
        url, data=message.encode(), method="POST", headers={"Title": title}
    )
    with urllib.request.urlopen(request, timeout=10):  # noqa: S310
        pass


class Notifier:
    def __init__(
        self,
        url: str | None,
        cooldown_hours: float,
        store: StateStore,
        post: Poster = post_webhook,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._url = url
        self._cooldown = timedelta(hours=cooldown_hours)
        self._store = store
        self._post = post
        self._now = now

    def notify(self, issue: str, message: str) -> None:
        """Send at most one message per issue per cooldown. Never raises."""
        if not self._url:
            return
        key = f"notify:{issue}"
        last = self._store.get(key)
        now = self._now()
        if last is not None and now - datetime.fromisoformat(last) < self._cooldown:
            return
        try:
            self._post(self._url, f"keepsake: {issue}", message)
        except Exception as exc:
            log.warning("notification failed: %s", exc)
            return
        self._store.set(key, now.isoformat())

    def resolved(self, issue: str) -> None:
        """Forget an issue so its next occurrence notifies immediately."""
        self._store.delete(f"notify:{issue}")
