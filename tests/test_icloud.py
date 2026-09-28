"""The daemon must never retry an Apple ID password (repeated failures lock the account)."""

from __future__ import annotations

from typing import Any

import pytest
from pyicloud.exceptions import PyiCloudFailedLoginException

from keepsake import icloud
from keepsake.backend import AuthError
from keepsake.state import ICLOUD_PASSWORD_BLOCKED, StateStore


class FakeApi:
    def __init__(self, password: str | None, attempts: list[str | None]) -> None:
        self.password = password
        self.attempts = attempts

    def authenticate(self) -> None:
        self.attempts.append(self.password)
        raise PyiCloudFailedLoginException("Invalid email/password combination.")


@pytest.fixture
def attempts(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    calls: list[str | None] = []

    def open_api(apple_id: str, password: str | None = None) -> Any:
        return FakeApi(password, calls)

    def keyring_password(apple_id: str) -> str:
        return "hunter2"

    monkeypatch.setattr(icloud, "open_api", open_api)
    monkeypatch.setattr(icloud, "keyring_password", keyring_password)
    return calls


def test_without_keyring_only_the_saved_session_is_tried(attempts: list[str | None]) -> None:
    store = StateStore(":memory:")
    with pytest.raises(AuthError, match="keepsake auth icloud"):
        icloud.connect("me@icloud.com", store, use_keyring=False)
    assert attempts == [None]


def test_keyring_password_is_tried_once_ever(attempts: list[str | None]) -> None:
    store = StateStore(":memory:")
    for _ in range(3):
        with pytest.raises(AuthError):
            icloud.connect("me@icloud.com", store, use_keyring=True)
    assert attempts == [None, "hunter2", None, None]
    assert store.get(ICLOUD_PASSWORD_BLOCKED) == "1"
