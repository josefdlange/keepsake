"""HTTP hardening for the unofficial clients: default timeouts and bounded rate-limit handling."""

from __future__ import annotations

import logging
import socket
import time
from typing import Any

from keepsake.backend import RetryLater

log = logging.getLogger(__name__)

HTTP_TIMEOUT = (10.0, 60.0)
# Backstop for sockets opened without a timeout (e.g. gpsoauth's per-call sessions).
SOCKET_TIMEOUT = 90.0


def install_socket_timeout() -> None:
    socket.setdefaulttimeout(SOCKET_TIMEOUT)


def harden_session(session: Any, timeout: tuple[float, float] = HTTP_TIMEOUT) -> None:
    """Wrap a requests.Session so every request has a timeout and HTTP 429 raises RetryLater.

    Raising on 429 keeps control out of gkeepapi's unbounded retry-with-sleep loop.
    """
    original = session.request

    def request(method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = timeout
        start = time.monotonic()
        response = original(method, url, *args, **kwargs)
        path = str(url).split("?")[0]
        log.debug(
            "HTTP %s %s -> %s in %.2fs",
            method,
            path,
            response.status_code,
            time.monotonic() - start,
        )
        if response.status_code == 429:
            retry_after: float | None
            try:
                retry_after = float(response.headers.get("Retry-After", ""))
            except ValueError:
                retry_after = None
            raise RetryLater(f"rate limited by {path}", retry_after)
        return response

    session.request = request
