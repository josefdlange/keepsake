"""Google Keep backend over gkeepapi (unofficial; see CLAUDE.md before changing).

Verified against gkeepapi 0.17.1 and live spikes:
- Writes mutate the local node tree and are sent by ``keep.sync()``, so this backend is buffered.
  ``ListItem.id`` is client-generated and final, so ``create()`` can return it immediately.
- Keep sends placeholder per-item timestamps (1970-01-01 + 1-2 ms), so ``modified_at`` is None.
- A cold authenticate + full sync is sub-second, so no gkeepapi state is persisted; ``discard()``
  simply drops the in-memory tree and the next call reconnects.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Generator

import gkeepapi
import requests
from gkeepapi import exception as gexc
from gkeepapi import node as gnode

from keepsake.backend import AuthError, BackendError, NotFoundError, RetryLater
from keepsake.httputil import harden_session
from keepsake.model import Item

log = logging.getLogger(__name__)


def new_keep() -> gkeepapi.Keep:
    keep = gkeepapi.Keep()
    # gkeepapi sets no HTTP timeouts and retries 429 forever; harden its private sessions.
    for api in (keep._keep_api, keep._reminders_api, keep._media_api):  # pyright: ignore[reportPrivateUsage]
        harden_session(api._session)  # pyright: ignore[reportPrivateUsage]
    return keep


@contextlib.contextmanager
def keep_errors(label: str) -> Generator[None]:
    """Time a gkeepapi call and translate its exceptions into backend errors."""
    start = time.monotonic()
    try:
        yield
    except BackendError:
        raise
    except gexc.LoginException as exc:
        raise AuthError(f"Keep login failed: {exc}") from exc
    except gexc.APIException as exc:
        code = getattr(exc, "code", None)
        if code in (401, 403):
            raise AuthError(f"Keep rejected credentials (HTTP {code})") from exc
        if code == 429:
            raise RetryLater("Keep rate limited") from exc
        raise BackendError(f"Keep API error (HTTP {code}): {exc}") from exc
    except gexc.KeepException as exc:
        raise BackendError(f"Keep error: {type(exc).__name__}: {exc}") from exc
    except requests.RequestException as exc:
        raise BackendError(f"Keep network error: {exc}") from exc
    finally:
        log.debug("keep.%s took %.2fs", label, time.monotonic() - start)


class KeepBackend:
    buffered = True

    def __init__(
        self,
        *,
        email: str,
        master_token: str,
        device_id: str,
        note_title: str,
        note_id: str | None = None,
        keep_factory: Callable[[], gkeepapi.Keep] = new_keep,
    ) -> None:
        self._email = email
        self._token = master_token
        self._device_id = device_id
        self._note_title = note_title
        self._note_id = note_id
        self._factory = keep_factory
        self._keep: gkeepapi.Keep | None = None

    # Connection and reads.
    def _sync(self) -> gkeepapi.Keep:
        if self._keep is None:
            keep = self._factory()
            with keep_errors("authenticate"):
                keep.authenticate(self._email, self._token, device_id=self._device_id)
            self._keep = keep
            return keep
        keep = self._keep
        try:
            with keep_errors("sync"):
                keep.sync()
        except BackendError:
            self._keep = None  # never reuse a tree in an unknown state
            raise
        return keep

    def note(self) -> gnode.List:
        if self._keep is None:
            raise BackendError("Keep is not connected")
        return find_note(self._keep, self._note_title, self._note_id)

    def snapshot(self) -> list[Item]:
        self._sync()
        return [Item(it.id, it.text, it.checked, None) for it in self.note().items]

    def poll(self) -> list[Item]:
        # Keep's sync is already incremental against the in-memory tree.
        return self.snapshot()

    # Writes (buffered until flush).
    def _item(self, backend_id: str) -> gnode.ListItem:
        node = self.note().get(backend_id)
        if not isinstance(node, gnode.ListItem) or node.deleted:
            raise NotFoundError(f"Keep item {backend_id} not found")
        return node

    def create(self, text: str, checked: bool) -> str:
        note = self.note()
        item = note.add(text, checked, note.settings.new_listitem_placement)
        return str(item.id)

    def update(
        self, backend_id: str, *, text: str | None = None, checked: bool | None = None
    ) -> None:
        item = self._item(backend_id)
        if text is not None and item.text != text:
            item.text = text
        if checked is not None and item.checked != checked:
            item.checked = checked

    def delete(self, backend_id: str) -> None:
        item = self._item(backend_id)
        for sub in list(item.subitems):
            item.dedent(sub)
        item.delete()

    def flush(self) -> None:
        if self._keep is None:
            return
        with keep_errors("flush"):
            self._keep.sync()

    def discard(self) -> None:
        self._keep = None


def find_note(keep: gkeepapi.Keep, title: str, note_id: str | None = None) -> gnode.List:
    """Find the synced checklist. Never returns 'nothing': a missing note is an error, so it can
    never be mistaken for an empty list."""
    if note_id:
        node = keep.get(note_id)
        if not isinstance(node, gnode.List) or node.trashed or node.deleted:
            raise BackendError(f"Keep note id {note_id} is not a live checklist")
        return node
    matches = [
        n
        for n in keep.all()
        if isinstance(n, gnode.List)
        and n.title.strip() == title.strip()
        and not n.trashed
        and not n.deleted
    ]
    if not matches:
        raise BackendError(f"no live Keep checklist titled {title!r}")
    if len(matches) > 1:
        ids = ", ".join(str(n.id) for n in matches)
        raise BackendError(
            f"{len(matches)} Keep checklists are titled {title!r}; set [keep] note_id to one "
            f"of: {ids}"
        )
    return matches[0]
