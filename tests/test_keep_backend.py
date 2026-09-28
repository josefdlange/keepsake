"""KeepBackend against a real, offline gkeepapi tree built from raw server-shaped nodes."""

from __future__ import annotations

from typing import Any

import gkeepapi
import pytest
from gkeepapi import exception as gexc

from keepsake.backend import AuthError, BackendError, NotFoundError, RetryLater
from keepsake.keep_backend import KeepBackend

NOTE_ID = "1646256009707.368896.1991954868"
EPOCH_TS = {
    "kind": "notes#timestamps",
    "created": "1970-01-01T00:00:00.001Z",
    "updated": "1970-01-01T00:00:00.002Z",
}


def raw_list(node_id: str, title: str, *, trashed: bool = False) -> dict[str, Any]:
    ts = {
        "kind": "notes#timestamps",
        "created": "2026-01-01T00:00:00.000Z",
        "updated": "2026-01-01T00:00:00.000Z",
    }
    if trashed:
        ts["trashed"] = "2026-02-01T00:00:00.000Z"
    return {
        "kind": "notes#node",
        "id": node_id,
        "serverId": f"s-{node_id}",
        "parentId": "root",
        "type": "LIST",
        "timestamps": ts,
        "title": title,
        "text": "",
        "nodeSettings": {
            "newListItemPlacement": "BOTTOM",
            "checkedListItemsPolicy": "GRAVEYARD",
            "graveyardState": "EXPANDED",
        },
        "annotationsGroup": {"kind": "notes#annotationsGroup"},
        "color": "DEFAULT",
        "isArchived": False,
        "isPinned": False,
        "sortValue": "1",
        "baseVersion": "1",
        "collaborators": [],
        "shareRequests": [],
        "labelIds": [],
    }


def raw_item(
    node_id: str, text: str, checked: bool = False, *, parent: str = NOTE_ID, sup: str = ""
) -> dict[str, Any]:
    # Shape copied from the live raw dump (1970 placeholder timestamps included).
    return {
        "kind": "notes#node",
        "id": node_id,
        "serverId": f"s-{node_id}",
        "parentId": parent,
        "parentServerId": f"s-{parent}",
        "type": "LIST_ITEM",
        "timestamps": dict(EPOCH_TS),
        "text": text,
        "checked": checked,
        "superListItemId": sup,
        "sortValue": "832831488",
        "baseVersion": "1",
        "nodeSettings": {
            "newListItemPlacement": "BOTTOM",
            "checkedListItemsPolicy": "GRAVEYARD",
            "graveyardState": "EXPANDED",
        },
        "annotationsGroup": {"kind": "notes#annotationsGroup"},
    }


class FakeKeep(gkeepapi.Keep):
    """Real node tree; network calls replaced."""

    def __init__(self, nodes: list[dict[str, Any]]) -> None:
        super().__init__()
        self._parseNodes(nodes)  # pyright: ignore[reportArgumentType]  (annotated dict, takes a list)
        self.syncs = 0
        self.sync_error: Exception | None = None
        self.auth_error: Exception | None = None

    def authenticate(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        if self.auth_error:
            raise self.auth_error

    def sync(self, resync: bool = False) -> None:
        self.syncs += 1
        if self.sync_error:
            raise self.sync_error


def standard_nodes() -> list[dict[str, Any]]:
    return [
        raw_list(NOTE_ID, "Groceries"),
        raw_list("other", "Groceries", trashed=True),
        raw_list("third", "Hardware"),
        raw_item("i1", "Green onion", True),
        raw_item("i2", "Milk"),
        raw_item("i3", "Oat milk", sup="i2"),  # indented under Milk
        raw_item("x1", "Nails", parent="third"),
    ]


def make(
    nodes: list[dict[str, Any]] | None = None, **kw: Any
) -> tuple[KeepBackend, list[FakeKeep]]:
    made: list[FakeKeep] = []

    def factory() -> gkeepapi.Keep:
        keep = FakeKeep(nodes if nodes is not None else standard_nodes())
        made.append(keep)
        return keep

    backend = KeepBackend(
        email="e",
        master_token="t",
        device_id="d",
        note_title=kw.get("title", "Groceries"),
        note_id=kw.get("note_id"),
        keep_factory=factory,
    )
    return backend, made


def test_snapshot_flattens_items_and_drops_placeholder_timestamps() -> None:
    backend, _ = make()
    items = {i.backend_id: i for i in backend.snapshot()}
    assert set(items) == {"i1", "i2", "i3"}  # trashed duplicate and other notes ignored
    assert items["i1"].checked and items["i1"].text == "Green onion"
    assert items["i3"].text == "Oat milk"  # sub-item flattened
    assert all(i.modified_at is None for i in items.values())


def test_missing_or_ambiguous_note_is_an_error_not_an_empty_list() -> None:
    backend, _ = make(title="Nope")
    with pytest.raises(BackendError, match="no live Keep checklist"):
        backend.snapshot()
    nodes = [*standard_nodes(), raw_list("dup", "Groceries")]
    backend, _ = make(nodes)
    with pytest.raises(BackendError, match="set \\[keep\\] note_id"):
        backend.snapshot()
    backend, _ = make(nodes, note_id=NOTE_ID)
    assert len(backend.snapshot()) == 3


def test_writes_are_buffered_until_flush() -> None:
    backend, made = make()
    backend.snapshot()
    keep = made[0]
    new_id = backend.create("Eggs", False)
    backend.update("i2", checked=True)
    backend.update("i1", text="Scallions")
    backend.delete("i3")
    note = keep.get(NOTE_ID)
    assert note is not None and note.dirty
    items = {i.backend_id: i for i in backend.poll()}
    assert items[new_id].text == "Eggs"
    assert items["i2"].checked
    assert items["i1"].text == "Scallions"
    assert "i3" not in items
    before = keep.syncs
    backend.flush()
    assert keep.syncs == before + 1


def test_update_or_delete_of_missing_item_raises_not_found() -> None:
    backend, _ = make()
    backend.snapshot()
    with pytest.raises(NotFoundError):
        backend.update("zzz", checked=True)
    backend.delete("i2")
    with pytest.raises(NotFoundError):
        backend.delete("i2")


def test_discard_drops_the_tree_so_unflushed_writes_never_land() -> None:
    backend, made = make()
    backend.snapshot()
    backend.create("Eggs", False)
    made[0].sync_error = gexc.APIException(500, "boom")
    with pytest.raises(BackendError):
        backend.flush()
    backend.discard()
    items = backend.snapshot()  # reconnects with a fresh tree
    assert len(made) == 2
    assert "Eggs" not in {i.text for i in items}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (gexc.LoginException("BadAuthentication"), AuthError),
        (gexc.APIException(401, "unauthorized"), AuthError),
        (gexc.APIException(429, "slow down"), RetryLater),
        (gexc.APIException(500, "oops"), BackendError),
        (gexc.ResyncRequiredException("resync"), BackendError),
    ],
)
def test_error_mapping(error: Exception, expected: type[Exception]) -> None:
    backend, made = make()
    backend.snapshot()
    made[0].sync_error = error
    with pytest.raises(expected):
        backend.snapshot()
    # A failed sync never reuses the tree.
    backend.snapshot()
    assert len(made) == 2


def test_auth_failure_on_connect() -> None:
    def factory() -> gkeepapi.Keep:
        keep = FakeKeep(standard_nodes())
        keep.auth_error = gexc.LoginException("BadAuthentication")
        return keep

    backend = KeepBackend(
        email="e", master_token="t", device_id="d", note_title="Groceries", keep_factory=factory
    )
    with pytest.raises(AuthError):
        backend.snapshot()
