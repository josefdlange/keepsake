"""Backend protocol, backend errors, and the in-memory FakeBackend used by tests."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from keepsake.model import Item


class BackendError(Exception):
    """A backend call failed. The pass is aborted; successful writes are kept."""


class AuthError(BackendError):
    """Credentials are missing, expired, or rejected. All writes must stop."""


class ConflictError(BackendError):
    """The item changed remotely under us (e.g. stale change tag). Skip; re-plan next pass."""


class NotFoundError(BackendError):
    """The item no longer exists remotely."""


class Backend(Protocol):
    """One side of the sync. Item ids are stable strings owned by the backend.

    A ``buffered`` backend applies writes locally and sends them on ``flush()``; ``create()``
    must still return the item's final id immediately. Write-through backends send each write
    as it is made, and their ``flush()``/``discard()`` are no-ops.
    """

    @property
    def buffered(self) -> bool: ...

    def snapshot(self) -> list[Item]:
        """Authoritative full read of every item in the synced list."""
        ...

    def poll(self) -> list[Item]:
        """Current view of every item, using cheap incremental changes where supported.

        Backends without incremental support simply return ``snapshot()``.
        """
        ...

    def create(self, text: str, checked: bool) -> str:
        """Create an item and return its backend id."""
        ...

    def update(
        self, backend_id: str, *, text: str | None = None, checked: bool | None = None
    ) -> None:
        """Change the given fields of an item. ``None`` leaves a field untouched."""
        ...

    def delete(self, backend_id: str) -> None: ...

    def flush(self) -> None:
        """Push any locally buffered writes. A no-op for backends that write through."""
        ...

    def discard(self) -> None:
        """Drop buffered writes that were not flushed. They must never be sent later."""
        ...


def _tick_clock(start: datetime | None = None) -> Callable[[], datetime]:
    base = start or datetime(2026, 1, 1, tzinfo=UTC)
    counter = itertools.count()
    return lambda: base + timedelta(seconds=next(counter))


class FakeBackend:
    """In-memory backend.

    By default writes apply immediately. With ``buffered=True`` (modelling Keep), writes are
    staged until ``flush()`` and dropped by ``discard()``; ``create()`` still returns the new id.

    ``clock`` supplies ``modified_at`` values; pass a shared clock to two fakes so that
    timestamps are comparable across sides. ``timestamps=False`` models a backend that
    exposes no modification times.
    """

    def __init__(
        self,
        prefix: str,
        *,
        clock: Callable[[], datetime] | None = None,
        timestamps: bool = True,
        buffered: bool = False,
    ) -> None:
        self.prefix = prefix
        self.buffered = buffered
        self.staged: list[Callable[[], None]] = []
        self.items: dict[str, Item] = {}
        self.clock = clock or _tick_clock()
        self.timestamps = timestamps
        self.calls: list[tuple[str, ...]] = []
        self.fail_on: dict[str, BackendError] = {}
        self.return_empty = False
        self._ids = itertools.count(1)

    # Direct manipulation, standing in for a human editing the list.
    def user_add(self, text: str, checked: bool = False) -> str:
        backend_id = f"{self.prefix}{next(self._ids)}"
        self.items[backend_id] = Item(backend_id, text, checked, self._now())
        return backend_id

    def user_edit(
        self, backend_id: str, *, text: str | None = None, checked: bool | None = None
    ) -> None:
        old = self.items[backend_id]
        self.items[backend_id] = Item(
            backend_id,
            old.text if text is None else text,
            old.checked if checked is None else checked,
            self._now(),
        )

    def user_delete(self, backend_id: str) -> None:
        del self.items[backend_id]

    def _now(self) -> datetime | None:
        return self.clock() if self.timestamps else None

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_on:
            raise self.fail_on[op]

    # Backend protocol.
    def snapshot(self) -> list[Item]:
        self._maybe_fail("snapshot")
        return [] if self.return_empty else list(self.items.values())

    def poll(self) -> list[Item]:
        self._maybe_fail("poll")
        return self.snapshot()

    def _write(self, apply: Callable[[], None]) -> None:
        if self.buffered:
            self.staged.append(apply)
        else:
            apply()

    def create(self, text: str, checked: bool) -> str:
        self._maybe_fail("create")
        backend_id = f"{self.prefix}{next(self._ids)}"

        def apply() -> None:
            self.items[backend_id] = Item(backend_id, text, checked, self._now())
            self.calls.append(("create", backend_id, text, str(checked)))

        self._write(apply)
        return backend_id

    def update(
        self, backend_id: str, *, text: str | None = None, checked: bool | None = None
    ) -> None:
        self._maybe_fail("update")
        if backend_id not in self.items:
            raise NotFoundError(backend_id)

        def apply() -> None:
            if backend_id in self.items:
                self.user_edit(backend_id, text=text, checked=checked)
                self.calls.append(("update", backend_id, str(text), str(checked)))

        self._write(apply)

    def delete(self, backend_id: str) -> None:
        self._maybe_fail("delete")
        if backend_id not in self.items:
            raise NotFoundError(backend_id)

        def apply() -> None:
            self.items.pop(backend_id, None)
            self.calls.append(("delete", backend_id))

        self._write(apply)

    def flush(self) -> None:
        self._maybe_fail("flush")
        staged, self.staged = self.staged, []
        for apply in staged:
            apply()

    def discard(self) -> None:
        self.staged = []
