"""
One log line per request, with its status, error code, and stage timings.

No line carries request content: not the transcript or its words, the
upload's name, or an error's detail, since a transcript can include
someone's name.
"""

import logging
import re
import secrets
import time
from typing import Optional

from starlette.datastructures import Headers, MutableHeaders

from app.telemetry import request_scope, to_ms

REQUEST_ID_HEADER = "x-request-id"
CALLER_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

logger = logging.getLogger("app.request")


def _caller_request_id(scope) -> Optional[str]:
    text = Headers(scope=scope).get(REQUEST_ID_HEADER)
    return text if text and CALLER_REQUEST_ID.fullmatch(text) else None


class RequestLog:
    """Pure ASGI rather than BaseHTTPMiddleware, so the endpoint shares this task's context."""

    def __init__(self, app, quiet: frozenset[str] = frozenset()) -> None:
        self.app = app
        self.quiet = quiet

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _caller_request_id(scope) or secrets.token_hex(8)
        status, error = 500, None
        started = time.perf_counter()

        async def send_with_id(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        with request_scope(request_id) as fields:
            try:
                await self.app(scope, receive, send_with_id)
            except Exception as e:
                error = type(e).__name__
                raise
            finally:
                if scope["path"] not in self.quiet or status >= 500:
                    _log_request(scope, status, error, time.perf_counter() - started, fields)


def _log_request(scope, status: int, error: Optional[str], seconds: float, fields: dict) -> None:
    level = logging.ERROR if status == 500 else logging.WARNING if status >= 500 else logging.INFO
    parts = [scope["method"], scope["path"], str(status)]
    if error is not None:
        parts.append(f"error={error}")
    if "code" in fields:
        parts.append(f"code={fields.pop('code')}")
    parts.append(f"total_ms={to_ms(seconds)}")
    parts += [f"{key}={value}" for key, value in fields.items()]
    logger.log(level, " ".join(parts))
