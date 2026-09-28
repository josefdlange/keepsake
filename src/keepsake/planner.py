"""Pure three-way sync planner. No I/O, no clock, no randomness.

``plan()`` compares each side's current items against the last-synced base and returns the
operations that bring both sides (and the base) into agreement. Every state change, including
pure bookkeeping, is an operation, so a dry run shows exactly what a real run would do.

Unmapped items that are already checked are ignored: completed items are never introduced to the
other side, and never used for pairing. Once an item is mapped, its checked state syncs normally.
"""

from __future__ import annotations

import enum
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from keepsake.model import BasePair, Item, Side, normalize


class TieBreaker(enum.StrEnum):
    KEEP = "keep"
    REMINDERS = "reminders"


@dataclass(frozen=True)
class Policy:
    # Text conflicts without usable timestamps go to this side.
    tie_breaker: TieBreaker = TieBreaker.KEEP


@dataclass(frozen=True)
class Create:
    """Create a copy of ``source_id`` (on the other side) on ``side`` and map the two."""

    side: Side
    text: str
    checked: bool
    source_id: str
    reason: str


@dataclass(frozen=True)
class Update:
    """Write fields on ``side``; the written fields become the pair's new base."""

    side: Side
    keep_id: str
    rem_id: str
    text: str | None
    checked: bool | None
    reason: str

    @property
    def backend_id(self) -> str:
        return self.keep_id if self.side is Side.KEEP else self.rem_id


@dataclass(frozen=True)
class Delete:
    """Delete the item on ``side`` and drop the pair's mapping."""

    side: Side
    keep_id: str
    rem_id: str
    text: str
    reason: str

    @property
    def backend_id(self) -> str:
        return self.keep_id if self.side is Side.KEEP else self.rem_id


@dataclass(frozen=True)
class Link:
    """Map two existing, previously unmapped items with the given base values."""

    keep_id: str
    rem_id: str
    text: str
    checked: bool
    reason: str


@dataclass(frozen=True)
class SetBase:
    """Both sides already agree on these fields; record that in the base."""

    keep_id: str
    rem_id: str
    text: str | None
    checked: bool | None
    reason: str


@dataclass(frozen=True)
class Unlink:
    """Drop a mapping whose items are gone from both sides."""

    keep_id: str
    rem_id: str
    reason: str


Operation = Create | Update | Delete | Link | SetBase | Unlink


def describe(op: Operation) -> str:
    """One-line human-readable form, used for dry runs and logs."""
    match op:
        case Create():
            mark = "x" if op.checked else " "
            return f"CREATE  {op.side:<9} [{mark}] {op.text!r}  ({op.reason})"
        case Update():
            fields: list[str] = []
            if op.text is not None:
                fields.append(f"text={op.text!r}")
            if op.checked is not None:
                fields.append(f"checked={op.checked}")
            return f"UPDATE  {op.side:<9} {op.backend_id} {' '.join(fields)}  ({op.reason})"
        case Delete():
            return f"DELETE  {op.side:<9} {op.backend_id} {op.text!r}  ({op.reason})"
        case Link():
            return f"LINK    {op.keep_id} <-> {op.rem_id} {op.text!r}  ({op.reason})"
        case SetBase():
            return f"BASE    {op.keep_id} <-> {op.rem_id}  ({op.reason})"
        case Unlink():
            return f"UNLINK  {op.keep_id} <-> {op.rem_id}  ({op.reason})"


def plan(
    keep_items: Iterable[Item],
    reminder_items: Iterable[Item],
    base: Iterable[BasePair],
    policy: Policy,
) -> list[Operation]:
    keep = {i.backend_id: i for i in keep_items}
    rem = {i.backend_id: i for i in reminder_items}
    ops: list[Operation] = []
    mapped_keep: set[str] = set()
    mapped_rem: set[str] = set()

    for pair in sorted(base, key=lambda p: (p.keep_id, p.rem_id)):
        mapped_keep.add(pair.keep_id)
        mapped_rem.add(pair.rem_id)
        k = keep.get(pair.keep_id)
        r = rem.get(pair.rem_id)
        if k is None and r is None:
            ops.append(Unlink(pair.keep_id, pair.rem_id, "deleted on both sides"))
        elif k is None:
            assert r is not None
            ops.append(_one_side_deleted(Side.KEEP, r, pair))
        elif r is None:
            ops.append(_one_side_deleted(Side.REMINDERS, k, pair))
        else:
            ops.extend(_reconcile(k, r, pair, policy))

    ops.extend(
        _match_unmapped(
            [i for i in keep.values() if i.backend_id not in mapped_keep],
            [i for i in rem.values() if i.backend_id not in mapped_rem],
            policy,
        )
    )
    return ops


def _changed(item: Item, pair: BasePair) -> bool:
    return _text_changed(item, pair) or item.checked != pair.checked


def _text_changed(item: Item, pair: BasePair) -> bool:
    # Blank text is never propagated: it is usually a half-finished edit.
    return item.text != pair.text and normalize(item.text) != ""


def _one_side_deleted(deleted_on: Side, survivor: Item, pair: BasePair) -> Operation:
    survivor_side = deleted_on.other
    if _changed(survivor, pair):
        return Create(
            side=deleted_on,
            text=survivor.text,
            checked=survivor.checked,
            source_id=survivor.backend_id,
            reason=f"deleted on {deleted_on} but modified on {survivor_side}; recreating",
        )
    return Delete(
        side=survivor_side,
        keep_id=pair.keep_id,
        rem_id=pair.rem_id,
        text=survivor.text,
        reason=f"deleted on {deleted_on}",
    )


def _later(k: Item, r: Item) -> Side | None:
    if k.modified_at is None or r.modified_at is None or k.modified_at == r.modified_at:
        return None
    return Side.KEEP if k.modified_at > r.modified_at else Side.REMINDERS


def _text_winner(k: Item, r: Item, policy: Policy) -> Side:
    return _later(k, r) or Side(policy.tie_breaker.value)


def _by_side(k: Item, r: Item, side: Side) -> Item:
    return k if side is Side.KEEP else r


def _emit_updates(
    k: Item,
    r: Item,
    writes: dict[Side, dict[str, str | bool]],
    reasons: dict[Side, list[str]],
) -> list[Operation]:
    ops: list[Operation] = []
    for side in (Side.KEEP, Side.REMINDERS):
        fields = writes[side]
        if not fields:
            continue
        text = fields.get("text")
        checked = fields.get("checked")
        ops.append(
            Update(
                side=side,
                keep_id=k.backend_id,
                rem_id=r.backend_id,
                text=text if isinstance(text, str) else None,
                checked=checked if isinstance(checked, bool) else None,
                reason="; ".join(reasons[side]),
            )
        )
    return ops


def _reconcile(k: Item, r: Item, pair: BasePair, policy: Policy) -> list[Operation]:
    writes: dict[Side, dict[str, str | bool]] = {Side.KEEP: {}, Side.REMINDERS: {}}
    reasons: dict[Side, list[str]] = {Side.KEEP: [], Side.REMINDERS: []}
    agreed_text: str | None = None
    agreed_checked: bool | None = None

    k_text, r_text = _text_changed(k, pair), _text_changed(r, pair)
    if k_text and r_text:
        if k.text == r.text:
            agreed_text = k.text
        else:
            winner = _text_winner(k, r, policy)
            writes[winner.other]["text"] = _by_side(k, r, winner).text
            reasons[winner.other].append(f"text changed on both sides; {winner} wins")
    elif k_text:
        writes[Side.REMINDERS]["text"] = k.text
        reasons[Side.REMINDERS].append("text changed on keep")
    elif r_text:
        writes[Side.KEEP]["text"] = r.text
        reasons[Side.KEEP].append("text changed on reminders")

    k_checked, r_checked = k.checked != pair.checked, r.checked != pair.checked
    if k_checked and r_checked:
        # Booleans: if both moved off the base they now agree.
        agreed_checked = k.checked
    elif k_checked:
        writes[Side.REMINDERS]["checked"] = k.checked
        reasons[Side.REMINDERS].append("checked changed on keep")
    elif r_checked:
        writes[Side.KEEP]["checked"] = r.checked
        reasons[Side.KEEP].append("checked changed on reminders")

    ops: list[Operation] = []
    if agreed_text is not None or agreed_checked is not None:
        ops.append(
            SetBase(
                k.backend_id,
                r.backend_id,
                agreed_text,
                agreed_checked,
                "same change on both sides",
            )
        )
    ops.extend(_emit_updates(k, r, writes, reasons))
    return ops


def _match_unmapped(
    keep_items: list[Item], rem_items: list[Item], policy: Policy
) -> list[Operation]:
    """Pair unmapped items by normalized text (one-to-one), create the rest on the other side.

    Checked and blank items are skipped.
    """
    by_key: dict[str, tuple[list[Item], list[Item]]] = defaultdict(lambda: ([], []))
    for item in keep_items:
        if not item.checked and (key := normalize(item.text)):
            by_key[key][0].append(item)
    for item in rem_items:
        if not item.checked and (key := normalize(item.text)):
            by_key[key][1].append(item)

    ops: list[Operation] = []
    for key in sorted(by_key):
        ks, rs = by_key[key]
        ks.sort(key=lambda i: (i.text, i.backend_id))
        rs.sort(key=lambda i: (i.text, i.backend_id))
        for k, r in _pair_up(ks, rs):
            ops.extend(_link(k, r, policy))
        for k in ks:
            ops.append(Create(Side.REMINDERS, k.text, k.checked, k.backend_id, "new on keep"))
        for r in rs:
            ops.append(Create(Side.KEEP, r.text, r.checked, r.backend_id, "new on reminders"))
    return ops


def _pair_up(ks: list[Item], rs: list[Item]) -> list[tuple[Item, Item]]:
    """Pair items, preferring exact text matches. Consumes paired items."""
    pairs: list[tuple[Item, Item]] = []
    for exact in (True, False):
        for k in list(ks):
            match = next((r for r in rs if not exact or r.text == k.text), None)
            if match is not None:
                pairs.append((k, match))
                ks.remove(k)
                rs.remove(match)
    return pairs


def _link(k: Item, r: Item, policy: Policy) -> list[Operation]:
    # Both items are unchecked (checked unmapped items are skipped). If the spellings differ, the
    # base takes the loser's text, so that if the follow-up write fails, the next pass still sees
    # the winner as the changed side.
    writes: dict[Side, dict[str, str | bool]] = {Side.KEEP: {}, Side.REMINDERS: {}}
    reasons: dict[Side, list[str]] = {Side.KEEP: [], Side.REMINDERS: []}
    base_text = k.text

    if k.text != r.text:
        winner = _text_winner(k, r, policy)
        base_text = _by_side(k, r, winner.other).text
        writes[winner.other]["text"] = _by_side(k, r, winner).text
        reasons[winner.other].append(f"matched by text; {winner} spelling wins")
    link = Link(k.backend_id, r.backend_id, base_text, False, "same text on both sides")
    return [link, *_emit_updates(k, r, writes, reasons)]
