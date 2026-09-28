"""iCloud session handling for pyicloud.

The daemon only ever reuses the trusted session saved by ``keepsake auth icloud``. A failed
password login can lock the Apple ID (Apple error -20209), so the daemon attempts at most one
automatic password login (and only with the keyring opt-in), then blocks further attempts until
``keepsake auth icloud`` is run again.
"""

from __future__ import annotations

import logging

from pyicloud import PyiCloudService

from keepsake import paths
from keepsake.backend import AuthError
from keepsake.httputil import harden_session
from keepsake.reminders_backend import ReminderService, reminders_errors
from keepsake.state import ICLOUD_PASSWORD_BLOCKED, StateStore

log = logging.getLogger(__name__)

KEYRING_SERVICE = "keepsake-icloud"


def open_api(apple_id: str, password: str | None = None) -> PyiCloudService:
    # authenticate=False: the constructor would otherwise read the system keyring, which raises
    # or blocks on a headless Pi. We authenticate explicitly afterwards.
    api = PyiCloudService(
        apple_id,
        password=password,
        cookie_directory=str(paths.icloud_dir()),
        authenticate=False,
    )
    harden_session(api.session)
    return api


def session_ready(api: PyiCloudService) -> bool:
    return api.is_trusted_session and not api.requires_2fa and not api.requires_2sa


def keyring_password(apple_id: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, apple_id)
    except Exception as exc:  # NoKeyringError, D-Bus failures, ...
        log.warning("keyring unavailable: %s", exc)
        return None


def store_keyring_password(apple_id: str, password: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, apple_id, password)


def connect(apple_id: str, store: StateStore, *, use_keyring: bool) -> ReminderService:
    """Non-interactive: reuse the saved trusted session and return the Reminders service."""
    try:
        api = open_api(apple_id)
        with reminders_errors("authenticate"):
            api.authenticate()
        if not session_ready(api):
            raise AuthError("iCloud session is not trusted")
    except AuthError as exc:
        api = _one_password_attempt(apple_id, store, use_keyring, exc)
    with reminders_errors("service"):
        service = api.reminders
    paths.tighten_permissions(paths.icloud_dir())
    return service


def _one_password_attempt(
    apple_id: str, store: StateStore, use_keyring: bool, cause: AuthError
) -> PyiCloudService:
    hint = "run `keepsake auth icloud`"
    if not use_keyring:
        raise AuthError(f"iCloud session unusable ({cause}); {hint}") from cause
    if store.get(ICLOUD_PASSWORD_BLOCKED):
        raise AuthError(
            f"iCloud session unusable and the one automatic password login was already used; {hint}"
        ) from cause
    password = keyring_password(apple_id)
    if not password:
        raise AuthError(f"iCloud session unusable and no keyring password; {hint}") from cause
    # Block first, so a crash mid-attempt can't lead to repeated attempts.
    store.set(ICLOUD_PASSWORD_BLOCKED, "1")
    log.warning("iCloud session unusable; making the single automatic password login attempt")
    api = open_api(apple_id, password)
    with reminders_errors("authenticate(password)"):
        api.authenticate()
    if not session_ready(api):
        raise AuthError(f"iCloud password login needs 2FA; {hint}")
    store.delete(ICLOUD_PASSWORD_BLOCKED)
    return api
