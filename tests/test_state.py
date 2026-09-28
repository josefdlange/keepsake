from __future__ import annotations

import stat
from pathlib import Path

import pytest

from keepsake.state import LOG_RETENTION, MIGRATIONS, StateStore


def test_migrates_to_latest_and_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "sub" / "state.db"
    store = StateStore(db)
    assert store.schema_version == len(MIGRATIONS)
    store.upsert_pair("k1", "r1", "Milk", False)
    store.close()
    reopened = StateStore(db)
    assert reopened.schema_version == len(MIGRATIONS)
    assert len(reopened.pairs()) == 1


def test_db_file_is_private(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    StateStore(db).close()
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_upsert_replaces_mappings_involving_either_id() -> None:
    store = StateStore(":memory:")
    store.upsert_pair("k1", "r1", "Milk", False)
    store.upsert_pair("k2", "r2", "Eggs", False)
    store.upsert_pair("k3", "r1", "Milk", True)  # r1 is remapped (recreate on keep)
    pairs = {(p.keep_id, p.rem_id, p.text, p.checked) for p in store.pairs()}
    assert pairs == {("k2", "r2", "Eggs", False), ("k3", "r1", "Milk", True)}


def test_update_base_fields_independently() -> None:
    store = StateStore(":memory:")
    store.upsert_pair("k1", "r1", "Milk", False)
    store.update_base("k1", "r1", checked=True)
    store.update_base("k1", "r1", text="Oat milk")
    (p,) = store.pairs()
    assert (p.text, p.checked) == ("Oat milk", True)
    store.remove_pair("k1", "r1")
    assert store.pairs() == []


def test_transaction_rolls_back_on_error_and_nests() -> None:
    store = StateStore(":memory:")
    with pytest.raises(RuntimeError), store.transaction():
        store.upsert_pair("k1", "r1", "Milk", False)
        with store.transaction():
            store.set("x", "1")
        raise RuntimeError
    assert store.pairs() == []
    assert store.get("x") is None
    with store.transaction():
        store.set("x", "2")
    assert store.get("x") == "2"


def test_kv_and_log_retention() -> None:
    store = StateStore(":memory:")
    store.set("cursor", "a")
    store.set("cursor", "b")
    assert store.get("cursor") == "b"
    store.delete("cursor")
    assert store.get("cursor") is None
    for i in range(LOG_RETENTION + 5):
        store.log("info", f"m{i}")
    recent = store.recent_log(2)
    assert [m for _, _, m in recent] == [f"m{LOG_RETENTION + 4}", f"m{LOG_RETENTION + 3}"]
    assert len(store.recent_log(LOG_RETENTION * 2)) == LOG_RETENTION
