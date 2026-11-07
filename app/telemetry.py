"""
Logging setup and the per-request context that log lines and stage timings use.

Standard library only, because the worker processes import it.
"""

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

LOG_FORMAT = "%(asctime)s %(levelname)s request=%(request_id)s %(message)s"

_request_id: ContextVar[str] = ContextVar("request_id", default="-")
_fields: ContextVar[Optional[dict]] = ContextVar("fields", default=None)


def configure_logging() -> None:
    """Send the app's and uvicorn's lines through one handler, without uvicorn's access log."""
    make_record = logging.getLogRecordFactory()

    def stamped(*args, **kwargs) -> logging.LogRecord:
        record = make_record(*args, **kwargs)
        record.request_id = _request_id.get()
        return record

    logging.setLogRecordFactory(stamped)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True


def note(**fields) -> None:
    """Add fields to the current request's log line; does nothing outside a request."""
    current = _fields.get()
    if current is not None:
        current.update(fields)


def to_ms(seconds: float) -> int:
    return round(seconds * 1000)


@contextmanager
def request_scope(request_id: str):
    """Collect the fields note() adds inside the block, and stamp its log lines with request_id."""
    fields: dict = {}
    id_token, fields_token = _request_id.set(request_id), _fields.set(fields)
    try:
        yield fields
    finally:
        _fields.reset(fields_token)
        _request_id.reset(id_token)


@contextmanager
def timed(stage: str):
    started = time.perf_counter()
    try:
        yield
    finally:
        note(**{f"{stage}_ms": to_ms(time.perf_counter() - started)})
