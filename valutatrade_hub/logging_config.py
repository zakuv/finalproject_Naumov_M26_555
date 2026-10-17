"""Настройка JSON-журнала операций с ротацией по размеру файла."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from valutatrade_hub.infra.settings import SettingsLoader

ACTION_LOGGER_NAME = "valutatrade.actions"
PARSER_LOGGER_NAME = "valutatrade.parser"


class JsonFormatter(logging.Formatter):
    """Одна операция — одна строка JSON с временем UTC."""

    def format(self, record: logging.LogRecord) -> str:
        """Добавить время и уровень к подготовленным полям операции."""
        if isinstance(record.msg, dict):
            payload = dict(record.msg)
        else:
            payload = {"message": record.getMessage()}
        payload["timestamp"] = datetime.fromtimestamp(record.created, UTC).isoformat()
        payload["level"] = record.levelname
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def configure_logging(settings: SettingsLoader | None = None) -> logging.Logger:
    """Настроить один файловый обработчик при запуске приложения."""
    if settings is None:
        from valutatrade_hub.infra.settings import SettingsLoader

        settings = SettingsLoader()

    if settings.get("log_format") != "json":
        raise ValueError("Поддерживается только формат журналов json.")

    log_path = Path(settings.get("log_file")).resolve()
    max_bytes = settings.get("log_max_bytes")
    backup_count = settings.get("log_backup_count")
    logger = _configure_file_logger(
        ACTION_LOGGER_NAME, log_path, max_bytes, backup_count, settings.get("log_level")
    )
    _configure_file_logger(
        PARSER_LOGGER_NAME,
        log_path.parent / "parser.log",
        max_bytes,
        backup_count,
        settings.get("log_level"),
    )
    return logger


def _configure_file_logger(
    name: str, log_path: Path, max_bytes: int, backup_count: int, level: str
) -> logging.Logger:
    """Создать или переиспользовать обработчик одного журнала с ротацией."""
    signature = (str(log_path), max_bytes, backup_count)
    logger = logging.getLogger(name)
    owned_handlers = [
        handler
        for handler in logger.handlers
        if getattr(handler, "_valutatrade_handler", False)
    ]
    matching = next(
        (
            handler
            for handler in owned_handlers
            if getattr(handler, "_valutatrade_signature", None) == signature
        ),
        None,
    )
    if matching is None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        matching = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        matching._valutatrade_handler = True
        matching._valutatrade_signature = signature
        logger.addHandler(matching)

    for handler in owned_handlers:
        if handler is not matching:
            logger.removeHandler(handler)
            handler.close()

    matching.setFormatter(JsonFormatter())
    logger.setLevel(level)
    logger.propagate = False
    return logger
