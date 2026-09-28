"""SQLite state store: pair mapping with base values, key/value state, and a sync log."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

from keepsake.model import BasePair

# Each entry migrates from user_version == index to index + 1. Append only.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE pairs (
        keep_id TEXT NOT NULL UNIQUE,
        rem_id  TEXT NOT NULL UNIQUE,
        text    TEXT NOT NULL,
        checked INTEGER NOT NULL,
        PRIMARY KEY (keep_id, rem_id)
    );
    CREATE TABLE kv (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE sync_log (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts      TEXT NOT NULL,
        level   TEXT NOT NULL,
        message TEXT NOT NULL
    );
    """,
]

LOG_RETENTION = 1000

# Well-known kv keys.
REMINDERS_CURSOR = "reminders_cursor"
REMINDERS_CURSOR_AT = "reminders_cursor_at"
KEEP_STATE = "keep_state"
LAST_SUCCESS = "last_success"
LAST_ERROR = "last_error"
LAST_ERROR_AT = "last_error_at"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, path: Path | str) -> None:
        if path != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not path.exists():
                path.touch(mode=0o600)
            path.chmod(0o600)
        # Autocommit mode; transactions are explicit via transaction().
        self._db = sqlite3.connect(str(path), isolation_level=None)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._depth = 0
        self._migrate()

    def close(self) -> None:
        self._db.close()

    def _migrate(self) -> None:
        version: int = self._db.execute("PRAGMA user_version").fetchone()[0]
        for target, script in enumerate(MIGRATIONS[version:], start=version + 1):
            with self.transaction():
                for statement in script.split(";"):
                    if statement.strip():
                        self._db.execute(statement)
                self._db.execute(f"PRAGMA user_version = {target}")

    @property
    def schema_version(self) -> int:
        return int(self._db.execute("PRAGMA user_version").fetchone()[0])

    @contextlib.contextmanager
    def transaction(self) -> Generator[None]:
        """Group writes atomically. Nested calls join the outermost transaction."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self._db.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")
        finally:
            self._depth = 0

    # Pairs.
    def pairs(self) -> list[BasePair]:
        rows = self._db.execute("SELECT keep_id, rem_id, text, checked FROM pairs").fetchall()
        return [BasePair(k, r, t, bool(c)) for k, r, t, c in rows]

    def upsert_pair(self, keep_id: str, rem_id: str, text: str, checked: bool) -> None:
        """Map two ids, replacing any existing mapping that involves either of them."""
        with self.transaction():
            self._db.execute("DELETE FROM pairs WHERE keep_id = ? OR rem_id = ?", (keep_id, rem_id))
            self._db.execute(
                "INSERT INTO pairs (keep_id, rem_id, text, checked) VALUES (?, ?, ?, ?)",
                (keep_id, rem_id, text, int(checked)),
            )

    def update_base(
        self,
        keep_id: str,
        rem_id: str,
        *,
        text: str | None = None,
        checked: bool | None = None,
    ) -> None:
        if text is not None:
            self._db.execute(
                "UPDATE pairs SET text = ? WHERE keep_id = ? AND rem_id = ?",
                (text, keep_id, rem_id),
            )
        if checked is not None:
            self._db.execute(
                "UPDATE pairs SET checked = ? WHERE keep_id = ? AND rem_id = ?",
                (int(checked), keep_id, rem_id),
            )

    def remove_pair(self, keep_id: str, rem_id: str) -> None:
        self._db.execute("DELETE FROM pairs WHERE keep_id = ? AND rem_id = ?", (keep_id, rem_id))

    # Key/value.
    def get(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def delete(self, key: str) -> None:
        self._db.execute("DELETE FROM kv WHERE key = ?", (key,))

    # Sync log.
    def log(self, level: str, message: str) -> None:
        with self.transaction():
            self._db.execute(
                "INSERT INTO sync_log (ts, level, message) VALUES (?, ?, ?)",
                (_now(), level, message),
            )
            self._db.execute(
                "DELETE FROM sync_log WHERE id <= (SELECT MAX(id) FROM sync_log) - ?",
                (LOG_RETENTION,),
            )

    def recent_log(self, limit: int = 20) -> list[tuple[str, str, str]]:
        rows = self._db.execute(
            "SELECT ts, level, message FROM sync_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [(str(ts), str(level), str(msg)) for ts, level, msg in rows]
