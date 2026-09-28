"""Command-line interface."""

from __future__ import annotations

import logging
import os
from typing import Annotated

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
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not verbose:
        # pyicloud and urllib3 are chatty at INFO and may log account details.
        for name in ("pyicloud", "urllib3", "gkeepapi"):
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
