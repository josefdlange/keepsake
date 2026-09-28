"""Domain model shared by all layers."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime


class Side(enum.StrEnum):
    KEEP = "keep"
    REMINDERS = "reminders"

    @property
    def other(self) -> Side:
        return Side.REMINDERS if self is Side.KEEP else Side.KEEP


@dataclass(frozen=True)
class Item:
    backend_id: str
    text: str
    checked: bool
    modified_at: datetime | None = None


def normalize(text: str) -> str:
    """Matching key: trim, collapse internal whitespace, casefold."""
    return " ".join(text.split()).casefold()


@dataclass(frozen=True)
class BasePair:
    """A mapped pair and its last-synced values: the base of the three-way diff."""

    keep_id: str
    rem_id: str
    text: str
    checked: bool
