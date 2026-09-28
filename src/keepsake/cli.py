"""Command-line interface. Filled in at step 5."""

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """Two-way sync between a Google Keep checklist and an Apple Reminders list."""


@app.command()
def version() -> None:
    """Print the keepsake version."""
    from keepsake import __version__

    typer.echo(__version__)
