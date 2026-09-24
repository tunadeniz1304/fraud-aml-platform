"""Structured logging (structlog over stdlib) with request-id correlation.

All ``logging.getLogger("fraud.*")`` loggers are rendered through structlog's
``ProcessorFormatter``: JSON in production (``LOG_JSON=true``), a readable
console format otherwise. The request id bound by the HTTP middleware (and the
transaction id bound by the pipeline) are merged into every record.

A :class:`SecretScrubFilter` removes any configured credential value from log
messages as defence in depth — keys must never reach a log sink.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from pathlib import Path

import structlog

from app.config import LOGS_DIR, Settings

_CONFIGURED = False


def _secret_values(settings: Settings) -> list[str]:
    values = [
        settings.jwt_secret.get_secret_value(),
        settings.admin_token.get_secret_value(),
        settings.service_api_key.get_secret_value(),
        settings.service_hmac_secret.get_secret_value(),
    ]
    try:
        from app.llm.config import LLMSettings

        llm = LLMSettings()
        values += [llm.api_key.get_secret_value(), llm.anthropic_api_key.get_secret_value()]
    except Exception:  # noqa: BLE001 - loglama yapılandırma hatasında çökmemeli
        return [v for v in values if v and len(v) >= 6]
    return [v for v in values if v and len(v) >= 6]


class SecretScrubFilter(logging.Filter):
    """Replace any known secret value in the rendered message with ``***``."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        scrubbed = message
        for secret in self._secrets:
            if secret in scrubbed:
                scrubbed = scrubbed.replace(secret, "***")
        if scrubbed != message:
            record.msg, record.args = scrubbed, ()
        return True


def configure_logging(settings: Settings, *, log_file: Path | None = None) -> None:
    """Idempotently configure root logging for the process."""
    global _CONFIGURED
    for stream in (sys.stdout, sys.stderr):  # Windows konsolu: UTF-8 zorla
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    scrub = SecretScrubFilter(_secret_values(settings))
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_anil3", False):
            root.removeHandler(handler)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(scrub)
        handler._anil3 = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    for noisy in ("httpx", "httpcore", "chromadb", "urllib3", "openai._base_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True
