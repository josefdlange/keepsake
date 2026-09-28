"""XDG locations for config, state, and secrets."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import IO


def config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "keepsake" / "config.toml"


def data_dir() -> Path:
    """Private data directory (mode 700) for state and secrets."""
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    path = base / "keepsake"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def state_db() -> Path:
    return data_dir() / "state.db"


def keep_token_file() -> Path:
    return data_dir() / "keep_master_token"


def keep_device_file() -> Path:
    return data_dir() / "keep_android_id"


def icloud_dir() -> Path:
    path = data_dir() / "icloud"
    path.mkdir(exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def read_secret(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except FileNotFoundError:
        return None


def write_secret(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value)
    path.chmod(0o600)


def tighten_permissions(root: Path) -> None:
    """Force 700 on directories and 600 on files under ``root`` (pyicloud writes with the
    process umask; this is a backstop in case something ran without ours)."""
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        path.chmod(0o700 if path.is_dir() else 0o600)


class AlreadyRunning(Exception):
    pass


def acquire_lock() -> IO[str]:
    """Hold an exclusive lock so only one sync (daemon or --once) writes at a time. Keep the
    returned handle open for the life of the process."""
    handle = (data_dir() / "lock").open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise AlreadyRunning("another keepsake sync is running") from None
    return handle
