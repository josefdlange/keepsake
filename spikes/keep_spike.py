# pyright: reportPrivateUsage=false
"""Exercise gkeepapi against a real account. Run on the Pi:

    uv run python spikes/keep_spike.py --email you@gmail.com --title "Groceries"

First run asks for an oauth_token (see README of gpsoauth: sign in at
https://accounts.google.com/EmbeddedSetup, click "I agree", copy the `oauth_token` cookie from
the browser's dev tools) and exchanges it for a master token stored under
~/.local/share/keepsake/spike/ (mode 600). Secrets are read with getpass, never from argv/env.

Creates one item named "keepsake-spike <timestamp>", checks it, and deletes it.
"""

from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys
import time
from importlib.metadata import version
from typing import Any

import gkeepapi
import gpsoauth
from _common import SPIKE_DIR, default_timeout, header, timed, write_secret
from gkeepapi import node as gnode

TOKEN_FILE = SPIKE_DIR / "keep_master_token"
DEVICE_FILE = SPIKE_DIR / "keep_android_id"


def android_id() -> str:
    if DEVICE_FILE.exists():
        return DEVICE_FILE.read_text().strip()
    value = secrets.token_hex(8)
    write_secret(DEVICE_FILE, value)
    return value


def master_token(email: str, device: str) -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    print("No master token yet. Paste the oauth_token cookie value (input hidden).")
    oauth_token = getpass.getpass("oauth_token: ").strip()
    response = timed(
        "gpsoauth.exchange_token", lambda: gpsoauth.exchange_token(email, oauth_token, device)
    )
    if "Token" not in response:
        print("Exchange failed. Response keys:", sorted(response))
        print("Error:", response.get("Error"), response.get("ErrorDetail"))
        sys.exit(1)
    token = response["Token"]
    write_secret(TOKEN_FILE, token)
    print(f"Master token saved to {TOKEN_FILE}")
    return token


def new_keep() -> gkeepapi.Keep:
    keep = gkeepapi.Keep()
    # gkeepapi sets no HTTP timeouts; patch its private sessions.
    for api in (keep._keep_api, keep._reminders_api, keep._media_api):
        default_timeout(api._session)
    return keep


def ts(dt: Any) -> str:
    return "None" if dt is None else dt.isoformat()


def show_item(it: gnode.ListItem) -> None:
    t = it.timestamps
    print(
        f"  id={it.id} server_id={it.server_id} checked={it.checked} indented={it.indented} "
        f"super={it.super_list_item_id!r} deleted={it.deleted} trashed={it.trashed}\n"
        f"    created={ts(t.created)} updated={ts(t.updated)} edited={ts(t.edited)} "
        f"text={it.text!r}"
    )


def find_note(keep: gkeepapi.Keep, title: str) -> gnode.List:
    candidates = [n for n in keep.all() if n.title.strip() == title.strip()]
    header(f"notes titled {title!r}")
    for n in candidates:
        print(
            f"  id={n.id} server_id={n.server_id} type={type(n).__name__} trashed={n.trashed} "
            f"deleted={n.deleted} archived={n.archived} collaborators={len(n.collaborators.all())}"
        )
    lists = [n for n in candidates if isinstance(n, gnode.List) and not n.trashed and not n.deleted]
    if len(lists) != 1:
        print(f"Expected exactly one live checklist, found {len(lists)}. All notes:")
        for n in keep.all():
            print(f"  {type(n).__name__:5} id={n.id} title={n.title!r}")
        sys.exit(1)
    return lists[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--title", required=True, help="exact title of the Keep checklist")
    parser.add_argument(
        "--wait-for-edit",
        action="store_true",
        help="pause so you can edit an item on Android, then show how timestamps changed",
    )
    args = parser.parse_args()

    header("versions")
    for pkg in ("gkeepapi", "gpsoauth"):
        print(f"  {pkg}=={version(pkg)}")

    device = android_id()
    token = master_token(args.email, device)

    header("cold authenticate + full sync")
    keep = new_keep()
    timed(
        "authenticate(sync=True) cold",
        lambda: keep.authenticate(args.email, token, device_id=device),
    )
    print(f"  top-level notes: {len(keep.all())}")

    state = keep.dump()
    size = len(json.dumps(state))
    print(f"  dump(): {size} bytes, {len(state['nodes'])} nodes")

    header("authenticate with restored state (incremental sync)")
    keep = new_keep()
    timed(
        "authenticate(state=dump, sync=True)",
        lambda: keep.authenticate(args.email, token, state=state, device_id=device),
    )
    timed("sync() with no changes", keep.sync)

    note = find_note(keep, args.title)
    header(f"items in {note.title!r} ({len(note.items)} live, {len(note.children)} incl. deleted)")
    for it in note.items:
        show_item(it)

    if args.wait_for_edit:
        before = {
            it.id: (it.text, it.checked, it.timestamps.updated, it.timestamps.edited)
            for it in note.items
        }
        input("\nEdit/check one item on Android now, wait a few seconds, then press Enter...")
        timed("sync() after external edit", keep.sync)
        header("items that changed")
        for it in note.items:
            if before.get(it.id) != (
                it.text,
                it.checked,
                it.timestamps.updated,
                it.timestamps.edited,
            ):
                print(f"  before: {before.get(it.id)}")
                show_item(it)

    header("write test")
    label = f"keepsake-spike {time.strftime('%H:%M:%S')}"
    item = note.add(label, False)
    print(f"  local id right after add(): {item.id} (server_id={item.server_id})")
    timed("sync() after add", keep.sync)
    show_item(item)

    item.checked = True
    timed("sync() after check", keep.sync)
    show_item(item)

    item.delete()
    timed("sync() after delete", keep.sync)
    still = note.get(item.id)
    print(f"  after delete: node still in tree={still is not None}")
    if isinstance(still, gnode.ListItem):
        show_item(still)

    fresh = new_keep()
    timed(
        "cold authenticate to verify",
        lambda: fresh.authenticate(args.email, token, device_id=device),
    )
    again = fresh.get(note.id)
    remaining = (
        [it for it in again.items if it.text == label] if isinstance(again, gnode.List) else []
    )
    print(f"  spike item visible in fresh sync: {bool(remaining)}")
    print("\nDone.")


if __name__ == "__main__":
    main()
