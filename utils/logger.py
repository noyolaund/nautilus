"""Structured logging with sensitive data masking and token tracking.

Produces:
  - Color-coded console output via Rich
  - JSON-Lines log files in the configured log directory
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.text import Text

# ---------------------------------------------------------------------------
# Sensitive data masking
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(password|passwd|pwd|secret|token|api[_-]?key|authorization)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9]{20,}", re.IGNORECASE),
]


def mask_sensitive(text: str) -> str:
    """Replace sensitive values in *text* with asterisks."""
    result = text
    for pattern in _SENSITIVE_PATTERNS:
        result = pattern.sub(lambda m: m.group(0)[:m.group(0).find(":")+1] + " ****"
                             if ":" in m.group(0)
                             else "****", result)
    return result


# ---------------------------------------------------------------------------
# Token tracker
# ---------------------------------------------------------------------------

class TokenTracker:
    """Accumulates LLM token usage across an entire suite run."""

    def __init__(self) -> None:
        self._total: int = 0
        self._per_step: dict[str, int] = {}

    def add(self, step_id: str, tokens: int) -> None:
        self._total += tokens
        self._per_step[step_id] = self._per_step.get(step_id, 0) + tokens

    @property
    def total(self) -> int:
        return self._total

    def for_step(self, step_id: str) -> int:
        return self._per_step.get(step_id, 0)

    def reset(self) -> None:
        self._total = 0
        self._per_step.clear()


# ---------------------------------------------------------------------------
# JSONL file handler
# ---------------------------------------------------------------------------

class JSONLHandler(logging.Handler):
    """Writes each log record as a single JSON line to a `.jsonl` file."""

    def __init__(self, log_dir: str | Path, run_id: str) -> None:
        super().__init__()
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        safe_run_id = re.sub(r"[^\w\-.]", "_", run_id)
        self._path = self._log_dir / f"{safe_run_id}.jsonl"
        self._file = open(self._path, "a", encoding="utf-8")  # noqa: SIM115

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry: dict[str, Any] = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": mask_sensitive(record.getMessage()),
            }
            for attr in ("test_id", "step_id", "status", "duration_ms", "tokens", "selector"):
                val = getattr(record, attr, None)
                if val is not None:
                    entry[attr] = val
            self._file.write(json.dumps(entry, default=str) + "\n")
            self._file.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self._file.close()
        super().close()


# ---------------------------------------------------------------------------
# Rich console handler
# ---------------------------------------------------------------------------

_console = Console(stderr=True)

_STATUS_STYLES: dict[str, tuple[str, str]] = {
    "RUNNING": ("bold cyan", "▸"),
    "PASS": ("bold green", "●"),
    "FAIL": ("bold red", "✖"),
    "ERROR": ("bold yellow", "⚠"),
    "SKIP": ("dim", "○"),
}


class RichStepHandler(logging.Handler):
    """Pretty-prints step-level log records to the console."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            status = getattr(record, "status", None)
            style, icon = _STATUS_STYLES.get(status or "", ("", " "))
            test_id = getattr(record, "test_id", "")
            step_id = getattr(record, "step_id", "")
            duration = getattr(record, "duration_ms", None)
            tokens = getattr(record, "tokens", None)

            ts = datetime.now().strftime("%H:%M:%S")
            msg = mask_sensitive(record.getMessage())
            text = Text()
            text.append(f"{ts} ", style="dim")

            # Resolution chain logs — format with 🔍 icon for visibility
            if status is None and record.levelname == "INFO" and any(
                kw in msg for kw in (
                    "matched:", "did not match:", "fallback", "candidates",
                    "Proxy", "Adjacent", "selector",
                )
            ):
                text.append("🔍 FIND  ", style="bold magenta")
                text.append(msg)
                _console.print(text)
                return

            text.append(f"{icon} {status or record.levelname:7s}", style=style)
            if test_id or step_id:
                text.append(f" [{test_id}/{step_id}]", style="dim")
            text.append(f" {msg}")
            if duration is not None:
                text.append(f"  ({duration:.0f}ms)", style="dim")
            selector = getattr(record, "selector", None)
            if tokens is not None and status not in ("RUNNING", None):
                if tokens > 0:
                    text.append(f" [{tokens} tokens]", style="dim cyan")
                elif selector and "cached" in str(selector):
                    text.append(" [cached]", style="dim green")
                else:
                    text.append(" [0 tokens]", style="dim")

            _console.print(text)

            error_msg = getattr(record, "error_message", None)
            if error_msg:
                err = Text()
                err.append("         ↳ ", style="dim")
                err.append(mask_sensitive(error_msg), style="red")
                _console.print(err)
        except Exception:
            self.handleError(record)


# ---------------------------------------------------------------------------
# Logger factory
# ---------------------------------------------------------------------------

def get_logger(
    name: str,
    log_dir: str = "logs",
    run_id: Optional[str] = None,
) -> logging.Logger:
    """Create a logger with both Rich console and JSONL file handlers."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logger.setLevel(getattr(logging, level, logging.INFO))
    logger.propagate = False

    logger.addHandler(RichStepHandler())

    if run_id:
        logger.addHandler(JSONLHandler(log_dir, run_id))

    return logger


def step_log(
    logger: logging.Logger,
    message: str,
    *,
    test_id: str = "",
    step_id: str = "",
    status: str = "RUNNING",
    duration_ms: Optional[float] = None,
    tokens: Optional[int] = None,
    selector: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    """Emit a step-level log record with structured extra fields."""
    extra = {
        "test_id": test_id,
        "step_id": step_id,
        "status": status,
        "duration_ms": duration_ms,
        "tokens": tokens,
        "selector": selector,
        "error_message": error_message,
    }
    level = logging.ERROR if status in ("FAIL", "ERROR") else logging.INFO
    logger.log(level, message, extra=extra)
