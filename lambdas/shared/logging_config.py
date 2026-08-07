"""Structured JSON logging with per-invocation correlation IDs."""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar
from typing import Any

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def new_correlation_id() -> str:
    cid = str(uuid.uuid4())
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> str:
    return _correlation_id.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlationId": _correlation_id.get() or None,
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _LOG_STD_KEYS:
                continue
            payload[key] = value
        return json.dumps(payload, default=str)


_LOG_STD_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message",
}


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if getattr(logger, "_zeroshift_configured", False):
        return logger
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    logger._zeroshift_configured = True  # type: ignore[attr-defined]
    return logger
