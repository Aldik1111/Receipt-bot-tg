"""Structured logs without financial fields or user-generated money text."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler

REDACT_KEYS = frozenset({
    "amount",
    "price",
    "total",
    "description",
    "store",
    "items",
    "raw_text",
    "init_data",
    "initData",
    "token",
    "password",
})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_data", None)
        if isinstance(extra, dict):
            payload["data"] = {
                key: "***" if key in REDACT_KEYS else value
                for key, value in extra.items()
            }
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = os.getenv("LOG_FORMAT", "text").strip().lower()
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    stream = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        stream.setFormatter(JsonFormatter())
    else:
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(stream)

    log_file = os.getenv("LOG_FILE", "").strip()
    if log_file:
        handler = TimedRotatingFileHandler(
            log_file,
            when="midnight",
            backupCount=max(1, int(os.getenv("LOG_RETAIN_DAYS", "14"))),
            encoding="utf-8",
        )
        handler.setFormatter(JsonFormatter() if fmt == "json" else stream.formatter)
        root.addHandler(handler)
