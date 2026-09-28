"""Shared helpers for the spike scripts. Not part of the keepsake package."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

SPIKE_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "keepsake/spike"
HTTP_TIMEOUT = (10.0, 60.0)


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_secret(path: Path, value: str) -> None:
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value)
    path.chmod(0o600)


def timed(label: str, fn: Callable[[], T]) -> T:
    start = time.monotonic()
    try:
        return fn()
    finally:
        print(f"  [time] {label}: {time.monotonic() - start:.2f}s")


def default_timeout(session: Any, timeout: tuple[float, float] = HTTP_TIMEOUT) -> None:
    """Give every request on a requests.Session a timeout unless one is passed explicitly."""
    original = session.request

    def request(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = timeout
        return original(*args, **kwargs)

    session.request = request


def header(title: str) -> None:
    print(f"\n=== {title} ===")
