"""One sync pass: fetch both sides, check safety guards, plan, execute."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from keepsake.backend import Backend
from keepsake.executor import ExecutionResult, execute
from keepsake.model import BasePair, Item, Side
from keepsake.planner import Delete, Operation, Policy, describe, plan
from keepsake.state import LAST_SUCCESS, StateStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Thresholds:
    # A pass is blocked if it would delete more than max_deletes items, or more than
    # max_delete_fraction of mapped items (the fraction rule only applies once at least
    # fraction_min_deletes items would be deleted, so tiny lists stay usable).
    # Deletes of checked items ("clear completed") never count.
    max_deletes: int = 10
    max_delete_fraction: float = 0.5
    fraction_min_deletes: int = 3


class SyncBlocked(Exception):
    """A safety guard refused the pass. Nothing was written. Override with --force."""

    def __init__(self, kind: str, message: str, ops: Sequence[Operation] = ()) -> None:
        super().__init__(message)
        self.kind = kind
        self.ops = list(ops)


@dataclass
class PassResult:
    ops: list[Operation]
    execution: ExecutionResult | None  # None for dry runs


def check_fetch(side: Side, items: Sequence[Item], base: Sequence[BasePair]) -> None:
    # An empty list is plausible only if everything mapped was already checked (the list was
    # cleared after shopping). Otherwise treat the fetch as broken.
    unchecked = sum(not p.checked for p in base)
    if not items and unchecked:
        raise SyncBlocked(
            "empty_fetch",
            f"{side} returned no items but {unchecked} unchecked items are mapped; "
            "treating the fetch as suspect and skipping this pass",
        )


def check_deletes(ops: Sequence[Operation], mapped: int, thresholds: Thresholds) -> None:
    deletes = sum(isinstance(op, Delete) and not op.checked for op in ops)
    too_many = deletes > thresholds.max_deletes
    too_large_share = (
        deletes >= thresholds.fraction_min_deletes
        and mapped > 0
        and deletes / mapped > thresholds.max_delete_fraction
    )
    if too_many or too_large_share:
        raise SyncBlocked(
            "delete_threshold",
            f"pass would delete {deletes} unchecked of {mapped} mapped items, over the configured "
            f"threshold (max {thresholds.max_deletes} or "
            f"{thresholds.max_delete_fraction:.0%}); run `keepsake sync --once --force` to apply",
            ops,
        )


def sync_pass(
    keep: Backend,
    reminders: Backend,
    store: StateStore,
    policy: Policy,
    thresholds: Thresholds,
    *,
    full: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> PassResult:
    base = store.pairs()
    keep_items = keep.snapshot() if full else keep.poll()
    rem_items = reminders.snapshot() if full else reminders.poll()
    log.debug("fetched %d keep / %d reminders items", len(keep_items), len(rem_items))

    if not force:
        check_fetch(Side.KEEP, keep_items, base)
        check_fetch(Side.REMINDERS, rem_items, base)

    ops = plan(keep_items, rem_items, base, policy)
    initial = store.get(LAST_SUCCESS) is None and not base
    if initial and any(isinstance(op, Delete) for op in ops):
        # Unreachable with an empty base; kept as a hard backstop.
        raise SyncBlocked("initial_delete", "refusing to delete anything on the first sync", ops)
    if not force:
        check_deletes(ops, len(base), thresholds)

    for op in ops:
        log.info("%s%s", "[dry-run] " if dry_run else "", describe(op))
    if dry_run:
        return PassResult(ops, None)

    execution = execute(ops, {Side.KEEP: keep, Side.REMINDERS: reminders}, store)
    store.set(LAST_SUCCESS, datetime.now(UTC).isoformat(timespec="seconds"))
    if ops:
        store.log("info", f"applied {len(execution.applied)} ops, skipped {len(execution.skipped)}")
    return PassResult(ops, execution)
