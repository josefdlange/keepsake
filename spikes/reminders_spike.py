"""Exercise pyicloud's CloudKit Reminders service against a real account. Run on the Pi:

    uv run python spikes/reminders_spike.py --apple-id you@icloud.com --list "Groceries"

The Apple ID password is read with getpass only when the cached session is not usable, and is
never stored. The trusted session lives in ~/.local/share/keepsake/spike/icloud (mode 700).

Creates one reminder named "keepsake-spike <timestamp>", completes it, and deletes it, printing
the iter_changes() events those writes produce and timing each call.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from importlib.metadata import version
from typing import Any

from _common import SPIKE_DIR, default_timeout, header, private_dir, timed
from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudFailedLoginException
from pyicloud.services.reminders import Reminder

COOKIE_DIR = SPIKE_DIR / "icloud"


def explain_login_failure(exc: BaseException) -> None:
    """pyicloud reports every signin/complete error as "Invalid email/password combination";
    print the chained causes so Apple's real status code and error body are visible."""
    print("\n  Login failed. Exception chain (Apple's real reason is usually last):")
    seen: BaseException | None = exc
    while seen is not None:
        print(f"   - {type(seen).__name__}: {str(seen)[:800]}")
        code = getattr(seen, "code", None)
        response = getattr(seen, "response", None)
        if code is not None:
            print(f"       code={code}")
        if response is not None:
            url = response.url.split("?")[0]
            print(f"       HTTP {response.status_code} {response.request.method} {url}")
            print(f"       body={response.text[:800]}")
        seen = seen.__cause__ or seen.__context__
    print("\n  Don't retry more than 2-3 times: repeated failures can lock the Apple ID.")


def login(apple_id: str) -> PyiCloudService:
    cookie_dir = str(private_dir(COOKIE_DIR))
    # authenticate=False: the constructor would otherwise consult the keyring, which on a
    # headless Pi raises NoKeyringError (or blocks on D-Bus).
    api = PyiCloudService(apple_id, cookie_directory=cookie_dir, authenticate=False)
    default_timeout(api.session)
    try:
        timed("authenticate() with cached session", api.authenticate)
        print(f"  cached session reused: trusted={api.is_trusted_session}")
    except PyiCloudFailedLoginException as exc:
        print(f"  cached session not usable ({exc}); password login required")
        password = getpass.getpass("Apple ID password (not stored): ")
        api = PyiCloudService(
            apple_id, password=password, cookie_directory=cookie_dir, authenticate=False
        )
        default_timeout(api.session)
        try:
            timed("authenticate() with password", api.authenticate)
        except PyiCloudFailedLoginException as login_exc:
            explain_login_failure(login_exc)
            sys.exit(1)

    if api.requires_2fa:
        header("2FA")
        print(f"  delivery method: {api.two_factor_delivery_method}")
        print(f"  security key names: {api.security_key_names}")
        if api.security_key_names:
            devices = api.fido2_devices
            print(f"  FIDO2 devices attached: {len(devices)}")
            if not devices:
                sys.exit("Security key required but none attached to this machine.")
            print("  Touch your security key...")
            api.confirm_security_key(devices[0])
        else:
            requested = api.request_2fa_code()
            print(f"  request_2fa_code() -> {requested}; notice: {api.two_factor_delivery_notice}")
            print(f"  delivery method now: {api.two_factor_delivery_method}")
            code = input("  Enter 2FA code: ").strip()
            if not api.validate_2fa_code(code):
                sys.exit("2FA code rejected.")
        if not api.is_trusted_session:
            print(f"  trust_session() -> {api.trust_session()}")
    elif api.requires_2sa:
        sys.exit("Legacy 2SA account; not supported by this spike.")
    print(f"  requires_2fa={api.requires_2fa} trusted={api.is_trusted_session}")
    return api


def show(rem: Reminder | None, indent: str = "    ") -> None:
    if rem is None:
        print(f"{indent}reminder=None")
        return
    print(
        f"{indent}id={rem.id} list_id={rem.list_id} title={rem.title!r} completed={rem.completed} "
        f"deleted={rem.deleted} parent={rem.parent_reminder_id}\n"
        f"{indent}modified={rem.modified} completed_date={rem.completed_date} "
        f"tag={rem.record_change_tag}"
    )


def changes(svc: Any, since: str, label: str) -> None:
    events = timed(
        f"iter_changes(since={label}) drained", lambda: list(svc.iter_changes(since=since))
    )
    print(f"  {len(events)} event(s) since {label}:")
    for e in events:
        print(f"   - type={e.type} reminder_id={e.reminder_id}")
        show(e.reminder, indent="       ")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apple-id", required=True)
    parser.add_argument("--list", required=True, help="exact name of the Reminders list")
    args = parser.parse_args()

    header("versions")
    print(f"  pyicloud=={version('pyicloud')}")

    header("login")
    api = login(args.apple_id)

    header("service + lists")
    svc = timed("api.reminders (PCS + service init)", lambda: api.reminders)
    lists = timed("lists() drained", lambda: list(svc.lists()))
    for lst in lists:
        print(
            f"  {lst.title!r} id={lst.id} count={lst.count} group={lst.is_group} "
            f"deleted={lst.deleted}"
        )
    matches = [lst for lst in lists if lst.title == args.list and not lst.deleted]
    if len(matches) != 1:
        sys.exit(f"Expected exactly one list named {args.list!r}, found {len(matches)}.")
    target = matches[0]

    header("cursor + snapshot")
    c0 = timed("sync_cursor() #1", svc.sync_cursor)
    print(f"  cursor length={len(c0)}")
    timed("sync_cursor() #2", svc.sync_cursor)
    snap = timed(
        "list_reminders(include_completed=True)",
        lambda: svc.list_reminders(target.id, include_completed=True),
    )
    top = [r for r in snap.reminders if not r.parent_reminder_id]
    print(
        f"  {len(snap.reminders)} reminders ({len(top)} top-level, "
        f"{sum(r.completed for r in snap.reminders)} completed)"
    )
    for r in snap.reminders[:5]:
        show(r)
    timed(
        "list_reminders(include_completed=False)",
        lambda: svc.list_reminders(target.id, include_completed=False),
    )
    changes(svc, c0, "C0 (no writes yet)")

    header("create")
    label = f"keepsake-spike {time.strftime('%H:%M:%S')}"
    created = timed("create()", lambda: svc.create(target.id, label))
    show(created)
    changes(svc, c0, "C0")
    c1 = timed("sync_cursor() after create", svc.sync_cursor)

    header("complete via get() + update()")
    fetched = timed("get()", lambda: svc.get(created.id))
    stale = fetched.model_copy(deep=True)
    fetched.completed = True
    timed("update(completed=True)", lambda: svc.update(fetched))
    print(f"  tag after update (on model): {fetched.record_change_tag}")
    changes(svc, c0, "C0")
    changes(svc, c1, "C1 (after create)")

    header("update with a stale record_change_tag")
    stale.title = label + " (stale write)"
    try:
        timed("update(stale)", lambda: svc.update(stale))
        print("  stale update SUCCEEDED (no optimistic-concurrency check)")
        show(timed("get() after stale update", lambda: svc.get(created.id)))
    except Exception as exc:
        print(f"  raised {type(exc).__module__}.{type(exc).__name__}: {exc}")
        print(f"  payload: {getattr(exc, 'payload', None)!r}")

    header("delete")
    current = timed("get()", lambda: svc.get(created.id))
    timed("delete()", lambda: svc.delete(current))
    changes(svc, c0, "C0")
    changes(svc, c1, "C1")
    c2 = timed("sync_cursor() after delete", svc.sync_cursor)
    changes(svc, c2, "C2 (steady-state empty poll)")
    try:
        show(timed("get() after delete", lambda: svc.get(created.id)))
    except Exception as exc:
        print(f"  get() after delete raised {type(exc).__name__}: {exc}")
    print("\nDone.")


if __name__ == "__main__":
    main()
