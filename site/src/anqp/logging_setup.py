"""Stdlib-only structured JSON logging. No third-party dependency.

Each log record is one JSON line — easy to grep, parse, and ingest.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
from typing import Any

from .config import settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Attach user-supplied "extra" fields, skipping LogRecord internals.
        for k, v in record.__dict__.items():
            if k in {
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "message", "module",
                "msecs", "msg", "name", "pathname", "process", "processName",
                "relativeCreated", "stack_info", "thread", "threadName", "taskName",
            }:
                continue
            try:
                json.dumps(v)
            except TypeError:
                v = repr(v)
            payload[k] = v
        return json.dumps(payload, ensure_ascii=False)


_configured = False


def configure() -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(settings.log_level)

    # Clear default handlers (e.g. uvicorn might add some)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt: logging.Formatter
    if settings.log_json:
        fmt = JsonFormatter()
    else:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    settings.log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        settings.log_dir / "anqp.log",
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Tame noisy libraries.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    configure()
    return logging.getLogger(name)
