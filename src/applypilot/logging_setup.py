"""Durable local logging with basic credential redaction."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from applypilot.config import PIPELINE_LOG_PATH

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._-]+"),
    re.compile(
        r"(?i)((?:OPENAI|ANTHROPIC|GEMINI|CAPSOLVER|LLM)_API_KEY\s*[:=]\s*)[^\s,;]+"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
)


def redact_text(value: str) -> str:
    """Remove common API credential formats from a log message."""
    redacted = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


def configure_file_logging(path: Path = PIPELINE_LOG_PATH) -> Path:
    """Attach one rotating, permission-restricted file handler to root logging."""
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)

    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "_openapplypilot_log_path", None) == str(path):
            return path

    handler = RotatingFileHandler(
        path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler._openapplypilot_log_path = str(path)  # type: ignore[attr-defined]
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    ))
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    path.chmod(0o600)
    return path
