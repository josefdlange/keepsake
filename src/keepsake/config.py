"""Configuration from ~/.config/keepsake/config.toml. Contains no secrets."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keepsake.engine import Thresholds
from keepsake.planner import Policy, TieBreaker

EXAMPLE = """\
[keep]
email = "you@gmail.com"
note_title = "Groceries"
# note_id = "..."          # only needed if several checklists share the title

[reminders]
apple_id = "you@icloud.com"
list_name = "Groceries"
use_keyring = false        # true: store the Apple ID password in the system keyring

[sync]
poll_interval = 60          # seconds between incremental passes
full_reconcile_interval = 3600
tie_breaker = "keep"        # "keep" or "reminders": who wins simultaneous text edits
max_deletes = 10            # block a pass deleting more unchecked items than this...
max_delete_fraction = 0.5   # ...or more than this share of mapped items
fraction_min_deletes = 3    # (the share rule applies from this many deletes)

[notify]
# webhook_url = "https://ntfy.sh/your-private-topic"
cooldown_hours = 4
"""


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    keep_email: str
    keep_note_title: str
    keep_note_id: str | None
    apple_id: str
    reminders_list: str
    use_keyring: bool
    poll_interval: float
    full_reconcile_interval: float
    policy: Policy
    thresholds: Thresholds
    webhook_url: str | None
    notify_cooldown_hours: float


def _get(table: dict[str, Any], key: str, kind: type, default: Any = None) -> Any:
    value = table.get(key, default)
    if value is None:
        return None
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if not isinstance(value, kind):
        raise ConfigError(f"{key} must be a {kind.__name__}, got {value!r}")
    return value


def _required(table: dict[str, Any], section: str, key: str) -> str:
    value = _get(table, key, str)
    if not value:
        raise ConfigError(f"[{section}] {key} is required")
    return str(value)


def load(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(
            f"no config at {path}; create it from this example:\n\n{EXAMPLE}"
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    keep = raw.get("keep", {})
    rem = raw.get("reminders", {})
    sync = raw.get("sync", {})
    notify = raw.get("notify", {})

    tie = _get(sync, "tie_breaker", str, "keep")
    try:
        tie_breaker = TieBreaker(tie)
    except ValueError:
        raise ConfigError('tie_breaker must be "keep" or "reminders"') from None

    config = Config(
        keep_email=_required(keep, "keep", "email"),
        keep_note_title=_required(keep, "keep", "note_title"),
        keep_note_id=_get(keep, "note_id", str) or None,
        apple_id=_required(rem, "reminders", "apple_id"),
        reminders_list=_required(rem, "reminders", "list_name"),
        use_keyring=_get(rem, "use_keyring", bool, False),
        poll_interval=_get(sync, "poll_interval", float, 60.0),
        full_reconcile_interval=_get(sync, "full_reconcile_interval", float, 3600.0),
        policy=Policy(tie_breaker=tie_breaker),
        thresholds=Thresholds(
            max_deletes=_get(sync, "max_deletes", int, 10),
            max_delete_fraction=_get(sync, "max_delete_fraction", float, 0.5),
            fraction_min_deletes=_get(sync, "fraction_min_deletes", int, 3),
        ),
        webhook_url=_get(notify, "webhook_url", str) or None,
        notify_cooldown_hours=_get(notify, "cooldown_hours", float, 4.0),
    )
    if config.poll_interval < 10:
        raise ConfigError("poll_interval must be at least 10 seconds")
    return config
