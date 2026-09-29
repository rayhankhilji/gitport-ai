"""Pure-ASGI middleware for the gitport API.

Written against the ASGI spec rather than BaseHTTPMiddleware so request
bodies are never buffered by the framework and non-HTTP scopes (lifespan,
websockets) pass through untouched. Error responses use the same
``{"error": {...}}`` envelope as the app's exception handlers.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings


async def _error_response(send, scope, status: int, type_: str, message: str,
                          extra_headers=()) -> None:
    """Emit a structured error response straight onto the ASGI channel."""
    payload = json.dumps({"error": {
        "type": type_,
        "message": message,
        "request_id": scope.get("gitport.request_id", ""),
    }}).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
            *extra_headers,
        ],
    })
    await send({"type": "http.response.body", "body": payload})


class RequestIDMiddleware:
    """Stamp every response with X-Request-ID and stash it on the scope.

    Runs outermost so even rejections from deeper middleware (413/429) carry
    the id, and so exception handlers can read ``scope["gitport.request_id"]``.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        scope["gitport.request_id"] = request_id

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((b"x-request-id", request_id.encode()))
            await send(message)

        await self.app(scope, receive, send_with_id)


class BodyLimitMiddleware:
    """Reject requests whose declared body exceeds ``max_bytes`` with 413.

    Content-Length is checked up front so oversized payloads are refused
    before a single byte of body is read. Bodies sent chunked (no length)
    cannot be pre-checked — front the service with a proxy limit for those.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name != b"content-length":
                continue
            try:
                length = int(value)
            except ValueError:
                break  # malformed header — let the framework deal with it
            if length > self.max_bytes:
                await _error_response(
                    send, scope, 413, "request_too_large",
                    f"body exceeds the {self.max_bytes} byte limit")
                return
            break
        await self.app(scope, receive, send)


class RateLimitMiddleware:
    """In-memory sliding-window rate limit, per client IP.

    Each worker keeps its own window — the right scope for single-node
    deploys; put a shared limiter (or proxy) in front for replicas. Windows
    for IPs that go idle linger until their next visit, which keeps the map
    bounded by the number of distinct clients seen. ``/healthz`` is exempt
    so probes never burn quota.
    """

    def __init__(self, app, rpm: int, window: float = 60.0):
        self.app = app
        self.rpm = rpm
        self.window = window
        self._hits: dict[str, deque[float]] = {}

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or self.rpm <= 0
                or scope.get("path") == "/healthz"):
            await self.app(scope, receive, send)
            return
        ip = (scope.get("client") or ("unknown", 0))[0]
        now = time.monotonic()
        hits = self._hits.setdefault(ip, deque())
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.rpm:
            retry = int(self.window - (now - hits[0])) + 1
            await _error_response(
                send, scope, 429, "rate_limited",
                f"limit is {self.rpm} requests per {int(self.window)}s",
                extra_headers=[(b"retry-after", str(retry).encode())])
            return
        hits.append(now)
        await self.app(scope, receive, send)


class _JsonFormatter(logging.Formatter):
    """One single-line JSON object per record — ingestible by aggregators."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def json_logging(cfg: Settings) -> None:
    """Route root logging through the JSON formatter when configured.

    Replaces (not adds) handlers, so calling it once per worker is enough and
    re-calling is harmless. No-op when ``cfg.log_format`` isn't "json".
    """
    if cfg.log_format != "json":
        return
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
