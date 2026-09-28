# pyright: reportPrivateUsage=false, reportUnknownLambdaType=false
"""Dump the raw server JSON for the synced list's items next to what the library parsed.

    uv run python spikes/raw_dump_spike.py keep --email you@gmail.com --title "Groceries"
    uv run python spikes/raw_dump_spike.py reminders --apple-id you@icloud.com --list "Groceries"

Read-only: nothing is written to either service. Reuses the credentials/session saved by
keep_spike.py and reminders_spike.py. The full raw bodies go to spikes/output/ (mode 600,
gitignored); stdout shows the first --limit items as raw JSON beside the parsed values.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import re
import sys
import time
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from _common import header, private_dir, timed

OUTPUT_DIR = Path(__file__).parent / "output"


def save(name: str, data: Any) -> Path:
    private_dir(OUTPUT_DIR)
    path = OUTPUT_DIR / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    path.chmod(0o600)
    return path


def pretty(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


# --- Keep --------------------------------------------------------------------------------


def dump_keep(args: argparse.Namespace) -> None:
    import keep_spike

    device = keep_spike.android_id()
    token = keep_spike.master_token(args.email, device)
    keep = keep_spike.new_keep()
    timed("authenticate", lambda: keep.authenticate(args.email, token, device_id=device))
    note = keep_spike.find_note(keep, args.title)

    # Re-fetch everything from version zero with the library's own low-level call, keeping the
    # unparsed response pages.
    pages: list[dict[str, Any]] = []
    version: str | None = None
    while True:
        page: dict[str, Any] = timed(
            "raw changes()", lambda v=version: keep._keep_api.changes(target_version=v)
        )
        pages.append(page)
        version = page.get("toVersion")
        if not page.get("truncated"):
            break

    raw_nodes = [n for p in pages for n in p.get("nodes", [])]
    raw_note = next((n for n in raw_nodes if n.get("id") == note.id), None)
    raw_items = [n for n in raw_nodes if n.get("parentId") == note.id]
    path = save("keep-raw", {"note": raw_note, "items": raw_items})
    print(f"\n  {len(raw_items)} raw item nodes (incl. deleted) saved to {path}")

    header("raw note node: timestamps only")
    print(pretty((raw_note or {}).get("timestamps")))

    header(f"first {args.limit} items: raw node vs parsed")
    parsed = {it.id: it for it in note.children}
    for raw in raw_items[: args.limit]:
        print(pretty(raw))
        it = parsed.get(raw.get("id", ""))
        if it is None:
            print("  parsed: <not in the parsed tree>")
        else:
            t = it.timestamps
            print(
                f"  parsed: text={it.text!r} checked={getattr(it, 'checked', None)} "
                f"created={t.created} updated={t.updated} edited={t.edited} "
                f"deleted={t.deleted} trashed={t.trashed}"
            )
        print("-" * 60)

    header("timestamp keys seen across all raw item nodes")
    keys: dict[str, set[str]] = {}
    for raw in raw_items:
        for k, v in (raw.get("timestamps") or {}).items():
            keys.setdefault(k, set()).add(str(v))
    for k, values in sorted(keys.items()):
        sample = sorted(values)[:3]
        print(f"  {k}: {len(values)} distinct value(s), e.g. {sample}")


# --- Reminders ---------------------------------------------------------------------------


def capture_cloudkit(session: Any, sink: list[dict[str, Any]]) -> None:
    """Record the raw JSON of every CloudKit records/changes response on this session."""
    original = session.request

    def request(method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        response = original(method, url, *args, **kwargs)
        if "/records/" in str(url) or "/changes/" in str(url):
            try:
                body = response.json()
            except ValueError:
                body = response.text
            sink.append({"url_path": str(url).split("?")[0].split("/private/")[-1], "body": body})
        return response

    session.request = request


def decode_document_independently(value: str) -> str:
    """Decode a TitleDocument without pyicloud's protobufs: base64, decompress, pull strings."""
    try:
        data = base64.b64decode(value + "=" * (-len(value) % 4))
    except ValueError as exc:
        return f"<base64 error: {exc}>"
    name = "raw"
    for codec, fn in (("zlib", zlib.decompress), ("gzip", gzip.decompress)):
        try:
            data = fn(data)
        except (zlib.error, OSError):
            continue
        name = codec
        break
    runs = re.findall(rb"[\x20-\x7e\xc2-\xf4][\x20-\x7e\x80-\xbf\xc2-\xf4]{1,}", data)
    return (
        f"{name}, {len(data)} bytes, printable runs: {[r.decode('utf-8', 'replace') for r in runs]}"
    )


def annotate_fields(fields: dict[str, Any]) -> dict[str, str]:
    """Human-readable view of timestamp and document fields, straight from the raw values."""
    notes: dict[str, str] = {}
    for key, field in fields.items():
        if not isinstance(field, dict):
            continue
        value = field.get("value")
        if field.get("type") == "TIMESTAMP" and isinstance(value, int | float):
            notes[key] = datetime.fromtimestamp(value / 1000, UTC).isoformat()
        elif key.endswith("Document") and isinstance(value, str):
            notes[key] = decode_document_independently(value)
    return notes


def dump_reminders(args: argparse.Namespace) -> None:
    import reminders_spike

    api = reminders_spike.login(args.apple_id)
    svc = timed("api.reminders", lambda: api.reminders)
    lists = timed("lists()", lambda: list(svc.lists()))
    target = next((lst for lst in lists if lst.title == args.list and not lst.deleted), None)
    if target is None:
        sys.exit(f"No list named {args.list!r}")

    captured: list[dict[str, Any]] = []
    capture_cloudkit(api.session, captured)
    result = timed(
        "list_reminders(include_completed=True)",
        lambda: svc.list_reminders(target.id, include_completed=True),
    )
    path = save("reminders-raw", captured)
    print(f"\n  {len(captured)} raw response(s) saved to {path}")

    raw_records = [
        rec
        for resp in captured
        if isinstance(resp["body"], dict)
        for rec in resp["body"].get("records", [])
        if rec.get("recordType") == "Reminder"
    ]
    record_types = sorted(
        {
            rec.get("recordType", "?")
            for resp in captured
            if isinstance(resp["body"], dict)
            for rec in resp["body"].get("records", [])
        }
    )
    print(f"  record types in response: {record_types}")
    print(f"  raw Reminder records: {len(raw_records)}; parsed reminders: {len(result.reminders)}")

    header(f"first {args.limit} reminders: raw record vs parsed")
    parsed = {r.id: r for r in result.reminders}
    for rec in raw_records[: args.limit]:
        print(pretty(rec))
        print("  decoded from raw:", pretty(annotate_fields(rec.get("fields", {}))))
        rem = parsed.get(rec.get("recordName", ""))
        if rem is None:
            print("  parsed: <not in parsed result (other list or filtered)>")
        else:
            print(f"  parsed: {pretty(rem.model_dump(mode='json'))}")
        print("-" * 60)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="side", required=True)
    k = sub.add_parser("keep")
    k.add_argument("--email", required=True)
    k.add_argument("--title", required=True)
    r = sub.add_parser("reminders")
    r.add_argument("--apple-id", required=True)
    r.add_argument("--list", required=True)
    for p in (k, r):
        p.add_argument("--limit", type=int, default=5, help="items to print to stdout")
    args = parser.parse_args()
    if args.side == "keep":
        dump_keep(args)
    else:
        dump_reminders(args)
    print("\nDone.")


if __name__ == "__main__":
    main()
