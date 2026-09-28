"""Apply planned operations to the backends and record the results in the state store.

Echo suppression: after each successful write the base is set to exactly what was written, so the
next pass sees our own writes as unchanged.

Writes to a write-through backend (Reminders) are recorded as soon as they succeed. Writes to a
buffered backend (Keep) are recorded only after its ``flush()`` succeeds; if the flush fails or is
skipped, the buffered writes are discarded. On failure, the executor stops issuing new writes,
records everything that did reach a server, then re-raises.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TypeVar

from keepsake.backend import AuthError, Backend, ConflictError, NotFoundError
from keepsake.model import Side
from keepsake.planner import (
    Create,
    Delete,
    Link,
    Operation,
    SetBase,
    Unlink,
    Update,
    describe,
)
from keepsake.state import StateStore

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class ExecutionResult:
    applied: list[Operation] = field(default_factory=list[Operation])
    skipped: list[tuple[Operation, str]] = field(default_factory=list[tuple[Operation, str]])


def execute(
    ops: Sequence[Operation],
    backends: Mapping[Side, Backend],
    store: StateStore,
) -> ExecutionResult:
    result = ExecutionResult()
    pending: dict[Side, list[tuple[Operation, Callable[[], None]]]] = {s: [] for s in Side}
    error: Exception | None = None

    # One transaction per pass, but it never rolls back: every write that reached a server is
    # recorded, even when the pass aborts. Losing such a record could orphan an item for good.
    with store.transaction():
        for op in ops:
            try:
                effect = _apply(op, backends, store)
            except (ConflictError, NotFoundError) as exc:
                log.warning("skipped %s: %s", describe(op), exc)
                result.skipped.append((op, str(exc)))
                continue
            except Exception as exc:
                error = exc
                log.error("aborting pass at %s: %s", describe(op), exc)
                break
            if effect is None:
                result.applied.append(op)
                continue
            side, record = effect
            if backends[side].buffered:
                pending[side].append((op, record))
            else:
                record()
                result.applied.append(op)

        for side in Side:
            if not pending[side]:
                continue
            backend = backends[side]
            if isinstance(error, AuthError):
                # Nothing more goes to the wire after an auth failure.
                reason = "discarded after auth failure"
            else:
                try:
                    _timed(f"{side}.flush", backend.flush)
                except Exception as exc:
                    log.error("flush of %s failed: %s", side, exc)
                    reason = f"flush failed: {exc}"
                    error = error or exc
                else:
                    for op, record in pending[side]:
                        record()
                        result.applied.append(op)
                    continue
            # Unflushed writes must never reach the server later, or they would be unrecorded.
            backend.discard()
            result.skipped.extend((op, reason) for op, _ in pending[side])

    if error is not None:
        raise error
    return result


def _timed(label: str, fn: Callable[[], T]) -> T:
    start = time.monotonic()
    try:
        return fn()
    finally:
        log.debug("%s took %.2fs", label, time.monotonic() - start)


def _apply(
    op: Operation, backends: Mapping[Side, Backend], store: StateStore
) -> tuple[Side, Callable[[], None]] | None:
    """Perform ``op``. Returns (side, state update to run after that side flushes), or None if
    the op was bookkeeping only and has already been recorded."""
    match op:
        case Link():
            store.upsert_pair(op.keep_id, op.rem_id, op.text, op.checked)
            return None
        case SetBase():
            store.update_base(op.keep_id, op.rem_id, text=op.text, checked=op.checked)
            return None
        case Unlink():
            store.remove_pair(op.keep_id, op.rem_id)
            return None
        case Create():
            new_id = _timed(
                f"{op.side}.create", lambda: backends[op.side].create(op.text, op.checked)
            )
            keep_id, rem_id = (
                (new_id, op.source_id) if op.side is Side.KEEP else (op.source_id, new_id)
            )
            return op.side, lambda: store.upsert_pair(keep_id, rem_id, op.text, op.checked)
        case Update():
            _timed(
                f"{op.side}.update",
                lambda: backends[op.side].update(op.backend_id, text=op.text, checked=op.checked),
            )
            return op.side, lambda: store.update_base(
                op.keep_id, op.rem_id, text=op.text, checked=op.checked
            )
        case Delete():
            with contextlib.suppress(NotFoundError):  # Already gone: the goal is met.
                _timed(f"{op.side}.delete", lambda: backends[op.side].delete(op.backend_id))
            return op.side, lambda: store.remove_pair(op.keep_id, op.rem_id)
