"""Interactive auth flows for `keepsake auth keep` and `keepsake auth icloud`.

Secrets are read with getpass and written only under the private data directory.
"""

from __future__ import annotations

import getpass
import secrets
import time

import gpsoauth
import typer
from pyicloud import PyiCloudService
from pyicloud.exceptions import (
    PyiCloudAPIResponseException,
    PyiCloudFailedLoginException,
    PyiCloudNoTrustedNumberAvailable,
)

from keepsake import icloud, paths
from keepsake.backend import BackendError
from keepsake.config import Config
from keepsake.keep_backend import KeepBackend, keep_errors
from keepsake.reminders_backend import reminders_errors
from keepsake.state import (
    ICLOUD_PASSWORD_BLOCKED,
    REMINDERS_LIST_ID,
    REMINDERS_LIST_NAME,
    StateStore,
)

KEEP_INSTRUCTIONS = """\
Google Keep needs a long-lived master token. To get one:

  1. In a desktop browser (a private window is fine), open
         https://accounts.google.com/EmbeddedSetup
  2. Sign in as {email}.
  3. Click "I agree". The page may then spin forever; that's expected.
  4. Open the browser's developer tools -> Application/Storage -> Cookies ->
     accounts.google.com, and copy the value of the `oauth_token` cookie
     (it starts with "oauth2_4/").
  5. Paste it below promptly; it expires within minutes.
"""


def keep_device_id() -> str:
    path = paths.keep_device_file()
    device = paths.read_secret(path)
    if device is None:
        device = secrets.token_hex(8)
        paths.write_secret(path, device)
    return device


def auth_keep(config: Config) -> None:
    typer.echo(KEEP_INSTRUCTIONS.format(email=config.keep_email))
    oauth_token = getpass.getpass("oauth_token (hidden): ").strip()
    if not oauth_token:
        raise typer.Exit(1)
    device = keep_device_id()
    with keep_errors("exchange_token"):
        response = gpsoauth.exchange_token(config.keep_email, oauth_token, device)
    token = response.get("Token")
    if not token:
        typer.echo(
            f"Token exchange failed: {response.get('Error')} {response.get('ErrorDetail', '')}\n"
            "Get a fresh oauth_token and try again.",
            err=True,
        )
        raise typer.Exit(1)

    typer.echo("Verifying by fetching the checklist...")
    backend = KeepBackend(
        email=config.keep_email,
        master_token=token,
        device_id=device,
        note_title=config.keep_note_title,
        note_id=config.keep_note_id,
    )
    try:
        items = backend.snapshot()
    except BackendError as exc:
        typer.echo(f"Verification failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    paths.write_secret(paths.keep_token_file(), token)
    open_items = sum(not i.checked for i in items)
    typer.echo(
        f"OK: found {config.keep_note_title!r} with {len(items)} items ({open_items} unchecked). "
        f"Master token saved to {paths.keep_token_file()}."
    )


def _explain_login_failure(exc: BaseException) -> None:
    """pyicloud reports every signin failure as "Invalid email/password combination"; show
    Apple's real status and error body."""
    typer.echo("Login failed. Apple's response:", err=True)
    seen: BaseException | None = exc
    while seen is not None:
        response = getattr(seen, "response", None)
        if response is not None:
            typer.echo(f"  HTTP {response.status_code}: {response.text[:500]}", err=True)
        seen = seen.__cause__ or seen.__context__
    typer.echo(
        "Do not retry repeatedly: failed logins can lock the Apple ID (error -20209). "
        "Check the password by signing in at icloud.com first.",
        err=True,
    )


def _complete_2fa(api: PyiCloudService) -> None:
    if api.requires_2sa and not api.requires_2fa:
        typer.echo("This account uses legacy two-step verification, which is not supported.")
        raise typer.Exit(1)
    if api.security_key_names:
        devices = api.fido2_devices
        typer.echo(f"Security key required ({', '.join(api.security_key_names)}).")
        if not devices:
            typer.echo("No FIDO2 security key is attached to this machine.", err=True)
            raise typer.Exit(1)
        typer.echo("Touch your security key...")
        api.confirm_security_key(devices[0])
    else:
        try:
            requested = api.request_2fa_code()
        except PyiCloudNoTrustedNumberAvailable:
            requested = False
        except PyiCloudAPIResponseException:
            typer.echo("Could not request a code; if a device already shows one, enter it.")
            api.use_existing_trusted_device_code()
            requested = True
        if not requested:
            typer.echo("Apple did not offer a code-based 2FA method for this login.", err=True)
            raise typer.Exit(1)
        method = api.two_factor_delivery_method
        typer.echo(
            "Approve the prompt on your Apple device, then enter the code shown."
            if method == "trusted_device"
            else "A code was sent by SMS."
            if method == "sms"
            else "Enter the 2FA code."
        )
        for attempt in range(3):
            code = typer.prompt("2FA code").strip()
            if api.validate_2fa_code(code):
                break
            if attempt == 2:
                typer.echo("2FA failed.", err=True)
                raise typer.Exit(1)
            typer.echo("Invalid code, try again.")
    if not api.is_trusted_session:
        api.trust_session()


def _login(config: Config) -> PyiCloudService:
    api = icloud.open_api(config.apple_id)
    try:
        with reminders_errors("authenticate"):
            api.authenticate()
        if icloud.session_ready(api):
            typer.echo("Reusing the saved trusted session.")
            return api
    except BackendError:
        pass

    password = getpass.getpass(f"Apple ID password for {config.apple_id} (hidden): ")
    api = icloud.open_api(config.apple_id, password)
    try:
        api.authenticate()
    except PyiCloudFailedLoginException as exc:
        _explain_login_failure(exc)
        raise typer.Exit(1) from exc
    if api.requires_2fa or api.requires_2sa:
        _complete_2fa(api)
    if not icloud.session_ready(api):
        typer.echo("Login did not produce a trusted session.", err=True)
        raise typer.Exit(1)
    if config.use_keyring:
        try:
            icloud.store_keyring_password(config.apple_id, password)
            typer.echo("Password stored in the system keyring (use_keyring = true).")
        except Exception as exc:
            typer.echo(f"Could not store the password in the keyring: {exc}", err=True)
    return api


def auth_icloud(config: Config, store: StateStore, list_id: str | None) -> None:
    api = _login(config)
    store.delete(ICLOUD_PASSWORD_BLOCKED)
    with reminders_errors("service"):
        svc = api.reminders

    if list_id is None:
        typer.echo("Looking up the list. pyicloud reads the whole zone for this; ~1 minute...")
        start = time.monotonic()
        with reminders_errors("lists"):
            lists = [
                lst for lst in svc.lists() if lst.title == config.reminders_list and not lst.deleted
            ]
        typer.echo(f"  ({time.monotonic() - start:.0f}s)")
        if len(lists) != 1:
            typer.echo(
                f"Expected exactly one Reminders list named {config.reminders_list!r}, "
                f"found {len(lists)}. Lists shared with you are not visible to pyicloud.",
                err=True,
            )
            raise typer.Exit(1)
        list_id = lists[0].id

    with reminders_errors("list_reminders"):
        result = svc.list_reminders(list_id, include_completed=True)
    top = [r for r in result.reminders if not r.parent_reminder_id and not r.deleted]
    store.set(REMINDERS_LIST_ID, list_id)
    store.set(REMINDERS_LIST_NAME, config.reminders_list)
    paths.tighten_permissions(paths.icloud_dir())
    open_items = sum(not r.completed for r in top)
    typer.echo(
        f"OK: {config.reminders_list!r} ({list_id}) has {len(top)} reminders "
        f"({open_items} open). Session saved under {paths.icloud_dir()}."
    )
