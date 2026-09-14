"""Structured logging with trace correlation.

Logs are JSON so the trace viewer and ops tooling can join on `trace_id`.
Agents call `bind_trace()` when they pick up a message; every log line emitted
while that binding is active carries the id automatically.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from common.config import settings

__all__ = ["bind_trace", "configure_logging", "current_trace_id", "get_logger"]

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)

#: LogRecord attributes that are never copied into the "extra" block.
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def current_trace_id() -> str | None:
    """The trace_id bound to the current context, if any."""
    return _trace_id.get()


@contextmanager
def bind_trace(trace_id: str) -> Iterator[None]:
    """Bind `trace_id` for the duration of the block."""
    token = _trace_id.set(trace_id)
    try:
        yield
    finally:
        _trace_id.reset(token)


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        trace_id = current_trace_id()
        if trace_id is not None:
            entry["trace_id"] = trace_id

        extra = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        if extra:
            entry["extra"] = extra

        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, default=str)


def configure_logging(level: str | None = None) -> None:
    """Install the JSON formatter on the root logger. Safe to call twice."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level or settings().log_level)


def get_logger(name: str) -> logging.Logger:
    """A logger for `name`."""
    return logging.getLogger(name)
