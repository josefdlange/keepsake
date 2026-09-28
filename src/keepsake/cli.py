"""Command-line interface."""

from __future__ import annotations

import logging
import os
from types import FrameType
from typing import IO, Annotated

import typer

from keepsake import config as config_mod
from keepsake import paths
from keepsake.httputil import install_socket_timeout

app = typer.Typer(no_args_is_help=True, add_completion=False)
auth_app = typer.Typer(no_args_is_help=True, help="Set up credentials for each service.")
app.add_typer(auth_app, name="auth")


def load_config() -> config_mod.Config:
    try:
        return config_mod.load(paths.config_path())
    except config_mod.ConfigError as exc:
        typer.echo(f"Config error: {exc}", err=True)
        raise typer.Exit(2) from exc


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:
    """Two-way sync between a Google Keep checklist and an Apple Reminders list."""
    # Everything we (and pyicloud) write is private: tokens, sessions, cookies, state.
    os.umask(0o077)
    install_socket_timeout()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # -v only raises our own loggers (which log per-call durations and URL paths). The libraries
    # stay at WARNING even then: their debug output includes URLs with account ids and auth
    # payloads, which must never reach the journal.
    logging.getLogger("keepsake").setLevel(logging.DEBUG if verbose else logging.INFO)
    for name in ("pyicloud", "urllib3", "gkeepapi", "gpsoauth"):
        logging.getLogger(name).setLevel(logging.WARNING)


@app.command()
def version() -> None:
    """Print the keepsake version."""
    from keepsake import __version__

    typer.echo(__version__)


@auth_app.command("keep")
def auth_keep() -> None:
    """Exchange a browser oauth_token for a Keep master token and verify it."""
    from keepsake.auth import auth_keep as run

    run(load_config())


@auth_app.command("icloud")
def auth_icloud(
    list_id: Annotated[
        str | None,
        typer.Option(help="Known list id (List/...), skips the ~1 minute list lookup"),
    ] = None,
) -> None:
    """Log in to iCloud (password + 2FA), save the trusted session, and find the list."""
    from keepsake.app import open_store
    from keepsake.auth import auth_icloud as run

    run(load_config(), open_store(), list_id)


@app.command()
def sync(
    once: Annotated[bool, typer.Option("--once", help="Run a single full pass (required)")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the plan; write nothing")
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Override the safety guards")] = False,
) -> None:
    """Run one full sync pass and print what it did (or would do)."""
    from keepsake import app as wiring
    from keepsake.backend import BackendError
    from keepsake.daemon import record_error
    from keepsake.engine import SyncBlocked, sync_pass
    from keepsake.planner import describe

    if not once:
        typer.echo("Use `keepsake sync --once` (or `keepsake run` for the daemon).", err=True)
        raise typer.Exit(2)
    config = load_config()
    lock = _lock()
    store = wiring.open_store()
    try:
        keep = wiring.keep_backend(config)
        reminders = wiring.reminders_backend(config, store)
        result = sync_pass(
            keep,
            reminders,
            store,
            config.policy,
            config.thresholds,
            full=True,
            dry_run=dry_run,
            force=force,
        )
    except SyncBlocked as blocked:
        typer.echo(f"BLOCKED: {blocked}", err=True)
        for op in blocked.ops:
            typer.echo(f"  {describe(op)}")
        raise typer.Exit(3) from blocked
    except BackendError as exc:
        if not dry_run:
            record_error(store, f"{type(exc).__name__}: {exc}")
        typer.echo(f"FAILED ({type(exc).__name__}): {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        lock.close()

    prefix = "Would apply" if dry_run else "Applied"
    typer.echo(f"{prefix} {len(result.ops)} operation(s):")
    for op in result.ops:
        typer.echo(f"  {describe(op)}")
    if result.execution and result.execution.skipped:
        typer.echo(f"Skipped {len(result.execution.skipped)} (will retry next pass):")
        for op, why in result.execution.skipped:
            typer.echo(f"  {describe(op)}: {why}")


def _lock() -> IO[str]:
    try:
        return paths.acquire_lock()
    except paths.AlreadyRunning as exc:
        typer.echo(f"{exc} (is the service active?)", err=True)
        raise typer.Exit(4) from exc


@app.command()
def run() -> None:
    """Run the sync daemon until stopped (SIGTERM/SIGINT)."""
    import signal
    import threading

    from keepsake import app as wiring
    from keepsake.daemon import Daemon
    from keepsake.notify import Notifier

    config = load_config()
    lock = _lock()
    store = wiring.open_store()
    stop = threading.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, request_stop)
    daemon = Daemon(
        config,
        store,
        Notifier(config.webhook_url, config.notify_cooldown_hours, store),
        keep_factory=lambda: wiring.keep_backend(config),
        reminders_factory=lambda: wiring.reminders_backend(config, store),
        stop=stop,
    )
    logging.getLogger(__name__).info(
        "keepsake running: poll every %.0fs, full reconcile every %.0fs",
        config.poll_interval,
        config.full_reconcile_interval,
    )
    try:
        daemon.run()
    finally:
        lock.close()
    logging.getLogger(__name__).info("stopped")


def _age(iso: str | None) -> str:
    from datetime import UTC, datetime

    if iso is None:
        return "never"
    seconds = (datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds()
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{iso} ({seconds / size:.0f}{unit} ago)"
    return f"{iso} ({seconds:.0f}s ago)"


@app.command()
def status() -> None:
    """Show auth state, counts, last sync, last error, and cursor age (no network)."""
    from keepsake import app as wiring
    from keepsake import state as st

    config = load_config()
    store = wiring.open_store()
    token = paths.read_secret(paths.keep_token_file()) is not None
    session = any(paths.icloud_dir().iterdir())
    list_ok = store.get(st.REMINDERS_LIST_NAME) == config.reminders_list
    blocked = store.get(st.ICLOUD_PASSWORD_BLOCKED) is not None
    last_error = store.get(st.LAST_ERROR)
    last_error_at = store.get(st.LAST_ERROR_AT)
    last_success = store.get(st.LAST_SUCCESS)
    auth_broken = (
        last_error is not None
        and last_error.startswith("auth:")
        and (last_success is None or (last_error_at or "") > last_success)
    )
    lines = [
        f"Keep:       {'token saved' if token else 'NOT SET UP (keepsake auth keep)'}"
        f" · note {config.keep_note_title!r} · {store.get(st.KEEP_ITEM_COUNT) or '?'} items",
        f"Reminders:  {'session saved' if session else 'NOT SET UP (keepsake auth icloud)'}"
        f" · list {config.reminders_list!r} {'' if list_ok else '(NOT RESOLVED) '}"
        f"· {store.get(st.REMINDERS_ITEM_COUNT) or '?'} items"
        + (" · automatic password login used up" if blocked else ""),
        f"Auth:       {'FAILING, see last error' if auth_broken else 'ok as of the last pass'}",
        f"Mapped:     {len(store.pairs())} pairs",
        f"Last sync:  {_age(last_success)}",
        f"Last error: {last_error or 'none'}"
        + (f" at {_age(last_error_at)}" if last_error_at else ""),
        f"Cursor:     updated {_age(store.get(st.REMINDERS_CURSOR_AT))}",
    ]
    typer.echo("\n".join(lines))
    recent = store.recent_log(5)
    if recent:
        typer.echo("Recent:")
        for ts, level, message in recent:
            typer.echo(f"  {ts} {level}: {message}")


@app.command()
def doctor() -> None:
    """Check config, permissions, connectivity, and auth on both sides; print versions."""
    import platform
    import stat
    import time
    from importlib.metadata import version as pkg_version

    from keepsake import app as wiring
    from keepsake.backend import BackendError

    ok = True

    def report(name: str, good: bool, detail: str) -> None:
        nonlocal ok
        ok = ok and good
        typer.echo(f"[{'ok' if good else 'FAIL'}] {name}: {detail}")

    typer.echo(f"python {platform.python_version()} on {platform.machine()} {platform.system()}")
    for pkg in ("keepsake", "gkeepapi", "gpsoauth", "pyicloud", "typer"):
        typer.echo(f"  {pkg}=={pkg_version(pkg)}")

    config = load_config()
    report("config", True, str(paths.config_path()))
    data = paths.data_dir()
    loose = [
        p
        for p in [data, *data.rglob("*")]
        if not p.is_symlink() and stat.S_IMODE(p.stat().st_mode) & 0o077
    ]
    report("permissions", not loose, "private" if not loose else f"too open: {loose[:3]}")

    store = wiring.open_store()
    report("state db", True, f"schema v{store.schema_version}, {len(store.pairs())} pairs")

    start = time.monotonic()
    try:
        items = wiring.keep_backend(config).snapshot()
        report("keep", True, f"{len(items)} items in {time.monotonic() - start:.1f}s")
    except BackendError as exc:
        report("keep", False, f"{type(exc).__name__}: {exc}")

    start = time.monotonic()
    try:
        reminders = wiring.reminders_backend(config, store)
        connected = time.monotonic() - start
        items = reminders.snapshot()
        read = time.monotonic() - start - connected
        report(
            "reminders",
            True,
            f"{len(items)} items (connect {connected:.1f}s, full read {read:.1f}s)",
        )
    except BackendError as exc:
        report("reminders", False, f"{type(exc).__name__}: {exc}")
    raise typer.Exit(0 if ok else 1)
