"""Table-driven planner tests: keep items, reminder items, base, expected ops."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from keepsake.model import BasePair, Item, normalize
from keepsake.planner import Delete, Policy, TieBreaker, describe, plan

from .helpers import K, R, item, pair, summarize


@dataclass
class Case:
    id: str
    keep: list[Item]
    rem: list[Item]
    base: list[BasePair]
    expected: list[tuple[object, ...]]
    policy: Policy = field(default_factory=Policy)


REM_WINS = Policy(tie_breaker=TieBreaker.REMINDERS)

CASES = [
    # --- steady state -------------------------------------------------------------------
    Case("no changes", [item("k1", "Milk")], [item("r1", "Milk")], [pair("k1", "r1", "Milk")], []),
    Case("empty everywhere", [], [], [], []),
    # --- rule 1: new on one side ------------------------------------------------------
    Case(
        "new on keep",
        [item("k1", "Milk")],
        [],
        [],
        [("create", R, "Milk", False, "k1")],
    ),
    Case(
        "new on reminders",
        [],
        [item("r1", "Eggs")],
        [],
        [("create", K, "Eggs", False, "r1")],
    ),
    Case(
        "unmapped checked items are ignored on both sides",
        [item("k1", "Bread", True)],
        [item("r1", "Eggs", True)],
        [],
        [],
    ),
    Case(
        "simultaneous add of the same text pairs instead of duplicating",
        [item("k1", "Milk")],
        [item("r1", "Milk")],
        [],
        [("link", "k1", "r1", "Milk", False)],
    ),
    Case(
        "simultaneous add, different spelling, no timestamps: keep wins by default",
        [item("k1", "Oat  milk ")],
        [item("r1", "oat milk")],
        [],
        [
            ("link", "k1", "r1", "oat milk", False),
            ("update", R, "r1", "Oat  milk ", None),
        ],
    ),
    Case(
        "simultaneous add, different spelling, tie-breaker set to reminders",
        [item("k1", "Oat Milk")],
        [item("r1", "oat milk")],
        [],
        [
            ("link", "k1", "r1", "Oat Milk", False),
            ("update", K, "k1", "oat milk", None),
        ],
        REM_WINS,
    ),
    Case(
        "simultaneous add, different spelling, newer timestamp wins over tie-breaker",
        [item("k1", "Oat Milk", t=1)],
        [item("r1", "oat milk", t=2)],
        [],
        [
            ("link", "k1", "r1", "Oat Milk", False),
            ("update", K, "k1", "oat milk", None),
        ],
    ),
    Case(
        "unmapped unchecked item is not paired with an unmapped checked one",
        [item("k1", "Milk", False)],
        [item("r1", "Milk", True)],
        [],
        [("create", R, "Milk", False, "k1")],
    ),
    Case(
        "unmapped duplicates pair one-to-one, the rest are created",
        [item("k1", "Milk"), item("k2", "Milk")],
        [item("r1", "Milk")],
        [],
        [
            ("link", "k1", "r1", "Milk", False),
            ("create", R, "Milk", False, "k2"),
        ],
    ),
    Case(
        "pairing prefers exact text over normalized match",
        [item("k1", "MILK"), item("k2", "milk")],
        [item("r1", "milk")],
        [],
        [
            ("link", "k2", "r1", "milk", False),
            ("create", R, "MILK", False, "k1"),
        ],
    ),
    Case(
        "a new duplicate of an already-mapped item is created, not paired",
        [item("k1", "Milk"), item("k2", "Milk")],
        [item("r1", "Milk")],
        [pair("k1", "r1", "Milk")],
        [("create", R, "Milk", False, "k2")],
    ),
    Case(
        "blank unmapped items are ignored",
        [item("k1", "   ")],
        [item("r1", "")],
        [],
        [],
    ),
    # --- rule 2: mapped, changed on one side ------------------------------------------
    Case(
        "text changed on keep",
        [item("k1", "Whole milk")],
        [item("r1", "Milk")],
        [pair("k1", "r1", "Milk")],
        [("update", R, "r1", "Whole milk", None)],
    ),
    Case(
        "text changed on reminders",
        [item("k1", "Milk")],
        [item("r1", "Whole milk")],
        [pair("k1", "r1", "Milk")],
        [("update", K, "k1", "Whole milk", None)],
    ),
    Case(
        "capitalization fix is a change (exact comparison, not normalized)",
        [item("k1", "milk")],
        [item("r1", "Milk")],
        [pair("k1", "r1", "Milk")],
        [("update", R, "r1", "milk", None)],
    ),
    Case(
        "checked on keep",
        [item("k1", "Milk", True)],
        [item("r1", "Milk")],
        [pair("k1", "r1", "Milk")],
        [("update", R, "r1", None, True)],
    ),
    Case(
        "unchecked on reminders",
        [item("k1", "Milk", True)],
        [item("r1", "Milk", False)],
        [pair("k1", "r1", "Milk", True)],
        [("update", K, "k1", None, False)],
    ),
    Case(
        "text cleared to blank on one side is not propagated",
        [item("k1", "")],
        [item("r1", "Milk")],
        [pair("k1", "r1", "Milk")],
        [],
    ),
    # --- rule 3: mapped, changed on both sides ----------------------------------------
    Case(
        "both checked at once: base only",
        [item("k1", "Milk", True)],
        [item("r1", "Milk", True)],
        [pair("k1", "r1", "Milk", False)],
        [("base", "k1", "r1", None, True)],
    ),
    Case(
        "both unchecked at once: base only",
        [item("k1", "Milk", False)],
        [item("r1", "Milk", False)],
        [pair("k1", "r1", "Milk", True)],
        [("base", "k1", "r1", None, False)],
    ),
    Case(
        "both renamed identically: base only",
        [item("k1", "Oat milk")],
        [item("r1", "Oat milk")],
        [pair("k1", "r1", "Milk")],
        [("base", "k1", "r1", "Oat milk", None)],
    ),
    Case(
        "both renamed differently, no timestamps: keep wins by default",
        [item("k1", "Oat milk")],
        [item("r1", "Soy milk")],
        [pair("k1", "r1", "Milk")],
        [("update", R, "r1", "Oat milk", None)],
    ),
    Case(
        "both renamed differently, no timestamps: reminders tie-breaker",
        [item("k1", "Oat milk")],
        [item("r1", "Soy milk")],
        [pair("k1", "r1", "Milk")],
        [("update", K, "k1", "Soy milk", None)],
        REM_WINS,
    ),
    Case(
        "both renamed differently, newer wins",
        [item("k1", "Oat milk", t=10)],
        [item("r1", "Soy milk", t=20)],
        [pair("k1", "r1", "Milk")],
        [("update", K, "k1", "Soy milk", None)],
    ),
    Case(
        "both renamed, only one side has a timestamp: tie-breaker",
        [item("k1", "Oat milk", t=10)],
        [item("r1", "Soy milk")],
        [pair("k1", "r1", "Milk")],
        [("update", R, "r1", "Oat milk", None)],
        Policy(),
    ),
    Case(
        "text changed on keep while checked on reminders: both propagate",
        [item("k1", "Oat milk", False)],
        [item("r1", "Milk", True)],
        [pair("k1", "r1", "Milk", False)],
        [
            ("update", K, "k1", None, True),
            ("update", R, "r1", "Oat milk", None),
        ],
    ),
    Case(
        "renamed on both and checked on both: text conflict plus base for checked",
        [item("k1", "Oat milk", True, t=1)],
        [item("r1", "Soy milk", True, t=2)],
        [pair("k1", "r1", "Milk", False)],
        [
            ("base", "k1", "r1", None, True),
            ("update", K, "k1", "Soy milk", None),
        ],
    ),
    # --- rule 4: deleted on one side, unchanged on the other ---------------------------
    Case(
        "deleted on keep",
        [],
        [item("r1", "Milk")],
        [pair("k1", "r1", "Milk")],
        [("delete", R, "r1")],
    ),
    Case(
        "deleted on reminders",
        [item("k1", "Milk", True)],
        [],
        [pair("k1", "r1", "Milk", True)],
        [("delete", K, "k1")],
    ),
    Case(
        "deleted on keep; reminders only changed whitespace-blank text (ignored): delete",
        [],
        [item("r1", " ")],
        [pair("k1", "r1", "Milk")],
        [("delete", R, "r1")],
    ),
    # --- rule 5: deleted on one side, modified on the other ----------------------------
    Case(
        "deleted on keep, renamed on reminders: recreate on keep",
        [],
        [item("r1", "Oat milk")],
        [pair("k1", "r1", "Milk")],
        [("create", K, "Oat milk", False, "r1")],
    ),
    Case(
        "deleted on reminders, checked on keep: recreate on reminders",
        [item("k1", "Milk", True)],
        [],
        [pair("k1", "r1", "Milk", False)],
        [("create", R, "Milk", True, "k1")],
    ),
    Case(
        "deleted on reminders, unchecked on keep: recreate",
        [item("k1", "Milk", False)],
        [],
        [pair("k1", "r1", "Milk", True)],
        [("create", R, "Milk", False, "k1")],
    ),
    # --- rule 6: deleted on both sides -------------------------------------------------
    Case(
        "deleted on both",
        [],
        [],
        [pair("k1", "r1", "Milk")],
        [("unlink", "k1", "r1")],
    ),
    # --- rule 8: initial sync ----------------------------------------------------------
    Case(
        "initial sync merges by normalized text, skips checked items, never deletes",
        [item("k1", "Milk"), item("k2", "Bread", True), item("k3", "eggs")],
        [item("r1", "milk "), item("r2", "Eggs"), item("r3", "Coffee")],
        [],
        [
            ("create", K, "Coffee", False, "r3"),
            ("link", "k3", "r2", "Eggs", False),
            ("update", R, "r2", "eggs", None),
            ("link", "k1", "r1", "milk ", False),
            ("update", R, "r1", "Milk", None),
        ],
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_plan(case: Case) -> None:
    ops = plan(case.keep, case.rem, case.base, case.policy)
    assert [summarize(op) for op in ops] == case.expected
    for op in ops:
        assert describe(op)


def test_initial_sync_never_deletes_even_with_lopsided_lists() -> None:
    keep = [item(f"k{i}", f"thing {i}") for i in range(20)]
    ops = plan(keep, [], [], Policy())
    assert not any(isinstance(op, Delete) for op in ops)
    assert len(ops) == 20


def test_plan_is_deterministic_regardless_of_input_order() -> None:
    keep = [item("k1", "Milk"), item("k2", "milk"), item("k3", "Eggs", True)]
    rem = [item("r1", "MILK"), item("r2", "eggs"), item("r3", "Bread")]
    base = [pair("k9", "r9", "Gone")]
    first = plan(keep, rem, base, Policy())
    second = plan(list(reversed(keep)), list(reversed(rem)), base, Policy())
    assert first == second


def test_normalize() -> None:
    assert normalize("  Oat \t Milk\n") == "oat milk"
    assert normalize("STRASSE") == normalize("straße")
