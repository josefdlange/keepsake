from __future__ import annotations

from datetime import UTC, datetime, timedelta

from keepsake.model import BasePair, Item, Side
from keepsake.planner import Create, Delete, Link, Operation, SetBase, Unlink, Update

T0 = datetime(2026, 1, 1, tzinfo=UTC)
K = Side.KEEP
R = Side.REMINDERS


def item(backend_id: str, text: str, checked: bool = False, t: int | None = None) -> Item:
    return Item(backend_id, text, checked, None if t is None else T0 + timedelta(seconds=t))


def pair(keep_id: str, rem_id: str, text: str, checked: bool = False) -> BasePair:
    return BasePair(keep_id, rem_id, text, checked)


def summarize(op: Operation) -> tuple[object, ...]:
    """Reason-free tuple form of an operation, for table-driven assertions."""
    match op:
        case Create():
            return ("create", op.side, op.text, op.checked, op.source_id)
        case Update():
            return ("update", op.side, op.backend_id, op.text, op.checked)
        case Delete():
            return ("delete", op.side, op.backend_id)
        case Link():
            return ("link", op.keep_id, op.rem_id, op.text, op.checked)
        case SetBase():
            return ("base", op.keep_id, op.rem_id, op.text, op.checked)
        case Unlink():
            return ("unlink", op.keep_id, op.rem_id)
