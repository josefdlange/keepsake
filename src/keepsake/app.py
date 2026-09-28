"""Wiring: build the state store and backends from config, secrets, and saved state."""

from __future__ import annotations

from keepsake import icloud, paths
from keepsake.auth import keep_device_id
from keepsake.backend import AuthError
from keepsake.config import Config
from keepsake.keep_backend import KeepBackend
from keepsake.reminders_backend import RemindersBackend
from keepsake.state import REMINDERS_LIST_ID, REMINDERS_LIST_NAME, StateStore


def open_store() -> StateStore:
    return StateStore(paths.state_db())


def keep_backend(config: Config) -> KeepBackend:
    token = paths.read_secret(paths.keep_token_file())
    if token is None:
        raise AuthError("no Keep master token; run `keepsake auth keep`")
    return KeepBackend(
        email=config.keep_email,
        master_token=token,
        device_id=keep_device_id(),
        note_title=config.keep_note_title,
        note_id=config.keep_note_id,
    )


def reminders_backend(config: Config, store: StateStore) -> RemindersBackend:
    list_id = store.get(REMINDERS_LIST_ID)
    if list_id is None or store.get(REMINDERS_LIST_NAME) != config.reminders_list:
        raise AuthError(
            f"Reminders list {config.reminders_list!r} not resolved; run `keepsake auth icloud`"
        )
    service = icloud.connect(config.apple_id, store, use_keyring=config.use_keyring)
    return RemindersBackend(service, list_id, store)
