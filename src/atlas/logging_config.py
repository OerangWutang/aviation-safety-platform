from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from atlas.config import get_settings

_STANDARD_LOG_RECORD_KEYS = set(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
}


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


class JsonLogFormatter(logging.Formatter):
    """Emit valid one-line JSON logs for container runtimes.

    ``logging`` callers can pass structured context through ``extra={...}``;
    those non-standard LogRecord attributes are preserved under ``extra`` so
    event IDs, source names, worker IDs, and similar operational fields survive
    ingestion into CloudWatch/Loki/ELK.
    """

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.message,
        }
        extra = {
            key: _json_safe(value)
            for key, value in record.__dict__.items()
            if key not in _STANDARD_LOG_RECORD_KEYS and not key.startswith("_")
        }
        if extra:
            payload["extra"] = extra
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def setup_logging() -> None:
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    # In containers/Gunicorn, handlers may already be installed. Avoid clearing
    # them blindly because process managers rely on those streams. Replace only
    # our own handler, and add one if none exists.
    for handler in list(root.handlers):
        if getattr(handler, "_atlas_json_handler", False):
            root.removeHandler(handler)

    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler._atlas_json_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)

    for handler in root.handlers:
        handler.setLevel(level)
        handler.setFormatter(JsonLogFormatter())

    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
