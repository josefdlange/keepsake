"""Randomized convergence: interleaved edits on both sides with repeated sync passes."""

from __future__ import annotations

import random
from collections import Counter

import pytest

from keepsake.backend import FakeBackend
from keepsake.engine import Thresholds, sync_pass
from keepsake.model import normalize
from keepsake.planner import Policy, TieBreaker
from keepsake.state import StateStore

VOCAB = ["milk", "eggs", "bread", "coffee", "apples", "oat milk", "rice", "tea"]


def variant(rng: random.Random, word: str) -> str:
    return rng.choice([word, word.title(), word.upper(), f" {word} ", word.replace(" ", "  ")])


def user_edit(rng: random.Random, backend: FakeBackend, intros: Counter[str]) -> None:
    ids = list(backend.items)
    action = rng.choice(["add", "add", "toggle", "toggle", "rename", "delete"])
    if action == "add" or not ids:
        word = rng.choice(VOCAB)
        intros[normalize(word)] += 1
        backend.user_add(variant(rng, word), checked=rng.random() < 0.1)
        return
    target = rng.choice(ids)
    if action == "toggle":
        backend.user_edit(target, checked=not backend.items[target].checked)
    elif action == "rename":
        word = rng.choice(VOCAB)
        intros[normalize(word)] += 1
        backend.user_edit(target, text=variant(rng, word))
    else:
        backend.user_delete(target)


@pytest.mark.parametrize("timestamps", [True, False])
@pytest.mark.parametrize("seed", range(40))
def test_random_edits_converge(seed: int, timestamps: bool) -> None:
    rng = random.Random(seed)
    keep = FakeBackend("k", timestamps=timestamps)
    rem = FakeBackend("r", clock=keep.clock, timestamps=timestamps)
    store = StateStore(":memory:")
    policy = Policy(tie_breaker=rng.choice(list(TieBreaker)))
    intros: Counter[str] = Counter()

    def run(full: bool = False) -> int:
        # force: random deletes may legitimately trip the guards, which are tested elsewhere.
        result = sync_pass(keep, rem, store, policy, Thresholds(), full=full, force=True)
        return len(result.ops)

    for _ in range(60):
        for _ in range(rng.randint(0, 4)):
            user_edit(rng, rng.choice([keep, rem]), intros)
        if rng.random() < 0.7:
            run(full=rng.random() < 0.2)

    for _ in range(3):
        if run() == 0:
            break
    assert run() == 0, "not stable: a quiet pass still produced operations"

    pairs = store.pairs()
    mapped_keep = {p.keep_id for p in pairs}
    mapped_rem = {p.rem_id for p in pairs}
    assert len(mapped_keep) == len(mapped_rem) == len(pairs)
    assert mapped_keep <= set(keep.items) and mapped_rem <= set(rem.items)

    # Everything unmapped is an item that was already checked when first seen (ignored by
    # design); every unchecked item is mapped.
    for backend, mapped in ((keep, mapped_keep), (rem, mapped_rem)):
        for backend_id, it in backend.items.items():
            if backend_id not in mapped:
                assert it.checked or not normalize(it.text)

    # Mapped items agree exactly, pair by pair.
    for p in pairs:
        k, r = keep.items[p.keep_id], rem.items[p.rem_id]
        assert (k.text, k.checked) == (r.text, r.checked) == (p.text, p.checked)

    # No runaway duplication: a text never appears on a side more often than it was entered.
    for backend in (keep, rem):
        counts = Counter(normalize(i.text) for i in backend.items.values())
        for key, n in counts.items():
            assert n <= intros[key], f"{key!r} duplicated: {n} > {intros[key]}"
