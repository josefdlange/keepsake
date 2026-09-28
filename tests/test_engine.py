"""Sync passes end to end over FakeBackends: guards, dry run, echo suppression, failures."""

from __future__ import annotations

import pytest

from keepsake.backend import AuthError, BackendError, ConflictError, FakeBackend
from keepsake.engine import PassResult, SyncBlocked, Thresholds, sync_pass
from keepsake.model import Side
from keepsake.planner import Policy
from keepsake.state import LAST_SUCCESS, StateStore


class World:
    def __init__(self, timestamps: bool = True) -> None:
        self.keep = FakeBackend("k", timestamps=timestamps, buffered=True)
        self.rem = FakeBackend("r", clock=self.keep.clock, timestamps=timestamps)
        self.store = StateStore(":memory:")

    def sync(self, *, thresholds: Thresholds | None = None, **kwargs: bool) -> PassResult:
        return sync_pass(
            self.keep, self.rem, self.store, Policy(), thresholds or Thresholds(), **kwargs
        )

    def texts(self, side: Side) -> list[tuple[str, bool]]:
        backend = self.keep if side is Side.KEEP else self.rem
        return sorted((i.text, i.checked) for i in backend.items.values())

    def assert_in_sync(self) -> None:
        assert self.texts(Side.KEEP) == self.texts(Side.REMINDERS)


def seeded(n: int) -> World:
    w = World()
    for i in range(n):
        w.keep.user_add(f"item {i}")
    w.sync()
    w.assert_in_sync()
    return w


def test_initial_sync_then_echo_suppression_over_two_passes() -> None:
    w = World()
    w.keep.user_add("Milk")
    w.rem.user_add("milk")
    w.rem.user_add("Eggs")
    first = w.sync()
    assert first.ops
    w.assert_in_sync()
    assert w.store.get(LAST_SUCCESS) is not None
    # Our own writes bumped modified_at on both sides; the next pass must still be a no-op.
    assert w.sync().ops == []
    assert w.sync(full=True).ops == []


def test_echo_suppression_after_update_propagation() -> None:
    w = seeded(3)
    kid = next(iter(w.keep.items))
    w.keep.user_edit(kid, text="Oat milk", checked=True)
    assert len(w.sync().ops) == 1
    w.assert_in_sync()
    assert w.sync().ops == []


def test_dry_run_writes_nothing() -> None:
    w = World()
    w.keep.user_add("Milk")
    result = w.sync(dry_run=True)
    assert result.execution is None
    assert len(result.ops) == 1
    assert w.rem.items == {}
    assert w.store.pairs() == []
    assert w.store.get(LAST_SUCCESS) is None


@pytest.mark.parametrize("side", [Side.KEEP, Side.REMINDERS])
def test_empty_fetch_guard_blocks_mass_delete(side: Side) -> None:
    w = seeded(3)
    (w.keep if side is Side.KEEP else w.rem).return_empty = True
    with pytest.raises(SyncBlocked) as info:
        w.sync()
    assert info.value.kind == "empty_fetch"
    assert len(w.keep.items) == len(w.rem.items) == 3
    assert len(w.store.pairs()) == 3


def test_empty_fetch_guard_can_be_forced() -> None:
    w = seeded(3)
    for kid in list(w.keep.items):
        w.keep.user_delete(kid)
    with pytest.raises(SyncBlocked):
        w.sync()
    w.sync(force=True)
    assert w.rem.items == {}
    assert w.store.pairs() == []


def test_empty_fetch_is_fine_when_nothing_is_mapped() -> None:
    w = World()
    w.rem.user_add("Milk")
    w.sync()
    w.assert_in_sync()


def test_delete_threshold_absolute() -> None:
    w = seeded(30)
    for kid in list(w.keep.items)[:11]:
        w.keep.user_delete(kid)
    with pytest.raises(SyncBlocked) as info:
        w.sync(thresholds=Thresholds(max_deletes=10, max_delete_fraction=1.0))
    assert info.value.kind == "delete_threshold"
    assert len(info.value.ops) == 11
    assert len(w.rem.items) == 30


def test_delete_threshold_fraction() -> None:
    w = seeded(4)
    for kid in list(w.keep.items)[:3]:
        w.keep.user_delete(kid)
    with pytest.raises(SyncBlocked):
        w.sync()
    w.sync(force=True)
    w.assert_in_sync()


def test_small_deletes_below_fraction_minimum_are_allowed() -> None:
    w = seeded(3)
    for kid in list(w.keep.items)[:2]:
        w.keep.user_delete(kid)
    w.sync()  # 2 of 3 is over 50% but under fraction_min_deletes
    w.assert_in_sync()


def test_clearing_checked_items_after_shopping_is_allowed() -> None:
    w = seeded(12)
    for kid in list(w.keep.items)[:5]:
        w.keep.user_edit(kid, checked=True)
    w.sync()
    for kid, it in list(w.keep.items.items()):
        if it.checked:
            w.keep.user_delete(kid)
    w.sync()
    w.assert_in_sync()
    assert len(w.rem.items) == 7


def test_delete_vs_modify_recreates_and_converges() -> None:
    w = seeded(2)
    kid = next(i for i, it in w.keep.items.items() if it.text == "item 0")
    rid = next(i for i, it in w.rem.items.items() if it.text == "item 0")
    w.keep.user_delete(kid)
    w.rem.user_edit(rid, text="Oat milk")
    w.sync()
    w.assert_in_sync()
    assert w.texts(Side.KEEP) == [("Oat milk", False), ("item 1", False)]
    p = next(p for p in w.store.pairs() if p.rem_id == rid)
    assert p.keep_id != kid and p.keep_id in w.keep.items
    assert w.sync().ops == []


def test_simultaneous_same_add_does_not_duplicate() -> None:
    w = seeded(2)
    w.keep.user_add("Coffee")
    w.rem.user_add("coffee")
    w.sync()
    w.assert_in_sync()
    assert len(w.keep.items) == 3


def test_checked_race_both_sides_check() -> None:
    w = seeded(1)
    (kid,) = w.keep.items
    (rid,) = w.rem.items
    w.keep.user_edit(kid, checked=True)
    w.rem.user_edit(rid, checked=True)
    result = w.sync()
    assert result.execution is not None
    assert w.keep.calls == [c for c in w.keep.calls if c[0] == "create"]  # no writes this pass
    assert w.sync().ops == []
    w.assert_in_sync()


def test_conflict_is_skipped_and_retried_next_pass() -> None:
    w = seeded(1)
    (kid,) = w.keep.items
    w.keep.user_edit(kid, text="Oat milk")
    w.rem.fail_on["update"] = ConflictError("stale tag")
    result = w.sync()
    assert result.execution is not None and len(result.execution.skipped) == 1
    del w.rem.fail_on["update"]
    w.sync()
    w.assert_in_sync()


def test_partial_failure_keeps_successful_writes_and_converges() -> None:
    w = World()
    for text in ("a", "b", "c"):
        w.keep.user_add(text)
    w.rem.user_add("d")
    w.keep.fail_on["create"] = BackendError("boom")
    with pytest.raises(BackendError):
        w.sync()
    # Reminders creates happened before Keep's failure and were recorded.
    assert len(w.store.pairs()) == 3
    del w.keep.fail_on["create"]
    w.sync()
    w.assert_in_sync()
    assert len(w.keep.items) == 4


def test_flush_failure_discards_keep_writes_and_heals_without_duplicates() -> None:
    w = seeded(1)
    w.rem.user_add("Eggs")
    w.keep.fail_on["flush"] = BackendError("sync failed")
    with pytest.raises(BackendError):
        w.sync()
    assert len(w.store.pairs()) == 1  # Keep create not recorded...
    assert len(w.keep.items) == 1  # ...and not applied later either.
    del w.keep.fail_on["flush"]
    w.sync()
    w.assert_in_sync()
    assert len(w.keep.items) == 2


def test_recreate_with_flush_failure_does_not_duplicate() -> None:
    w = seeded(2)
    kid = next(i for i, it in w.keep.items.items() if it.text == "item 0")
    rid = next(i for i, it in w.rem.items.items() if it.text == "item 0")
    w.keep.user_delete(kid)
    w.rem.user_edit(rid, text="Oat milk")
    w.keep.fail_on["flush"] = BackendError("sync failed")
    with pytest.raises(BackendError):
        w.sync()
    del w.keep.fail_on["flush"]
    w.sync()
    w.assert_in_sync()
    assert w.texts(Side.KEEP) == [("Oat milk", False), ("item 1", False)]
    assert w.sync().ops == []


def test_reminders_write_is_recorded_before_later_auth_failure() -> None:
    w = World()
    w.keep.user_add("a")
    w.keep.user_add("b")
    w.sync(dry_run=True)
    calls = 0

    def create_then_fail(text: str, checked: bool) -> str:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AuthError("expired")
        return FakeBackend.create(w.rem, text, checked)

    w.rem.create = create_then_fail  # type: ignore[method-assign]
    with pytest.raises(AuthError):
        w.sync()
    assert len(w.store.pairs()) == 1
    (p,) = w.store.pairs()
    # The user completes that reminder while we wait for re-auth. It must stay mapped, so the
    # check syncs back to Keep instead of the Keep item being created a second time.
    w.rem.user_edit(p.rem_id, checked=True)
    del w.rem.create
    w.sync()
    w.assert_in_sync()
    assert len(w.rem.items) == 2
    assert w.keep.items[p.keep_id].checked


def test_unexpected_exception_still_records_successful_writes() -> None:
    w = World()
    w.keep.user_add("a")
    w.keep.user_add("b")
    calls = 0

    def create_then_crash(text: str, checked: bool) -> str:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("library bug")
        return FakeBackend.create(w.rem, text, checked)

    w.rem.create = create_then_crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        w.sync()
    assert len(w.store.pairs()) == 1


def test_auth_error_stops_writes_and_propagates() -> None:
    w = World()
    w.keep.user_add("a")
    w.rem.user_add("b")
    w.rem.fail_on["create"] = AuthError("expired")
    with pytest.raises(AuthError):
        w.sync()
    assert w.store.get(LAST_SUCCESS) is None


def test_fetch_error_propagates_without_writes() -> None:
    w = seeded(2)
    w.rem.fail_on["poll"] = AuthError("expired")
    with pytest.raises(AuthError):
        w.sync()
    assert all(c[0] == "create" for c in w.keep.calls + w.rem.calls)
