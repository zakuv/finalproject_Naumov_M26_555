"""Журналирование операций без сохранения паролей и исходных аргументов."""

import logging
import math
from collections.abc import Callable
from functools import wraps
from inspect import signature
from typing import ParamSpec, TypeVar

from valutatrade_hub.core.exceptions import ApiRequestError, StorageError
from valutatrade_hub.core.utils import validate_currency_code
from valutatrade_hub.logging_config import ACTION_LOGGER_NAME

P = ParamSpec("P")
R = TypeVar("R")


def _log_number(value: object) -> int | float | None:
    """Пропустить только обычные конечные числа, исключив bool."""
    if type(value) not in (int, float):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _log_currency(value: object) -> str | None:
    """Оставить в журнале только корректно сформированный код валюты."""
    try:
        return validate_currency_code(value)
    except ValueError:
        return None


def _error_message(error: Exception) -> str:
    """Сохранить сообщения предметных ошибок без деталей неожиданных сбоев."""
    if isinstance(error, (ValueError, PermissionError, ApiRequestError, StorageError)):
        return str(error)
    return "Непредвиденная ошибка при выполнении операции."


def log_action(
    action_name: str, verbose: bool = False
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Записать результат операции и передать исключение вызывающему коду."""

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        """Сохранить сигнатуру метода для выборочного чтения аргументов."""
        method_signature = signature(func)

        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            """Журналировать только разрешённые поля одной операции."""
            try:
                arguments = method_signature.bind_partial(*args, **kwargs).arguments
            except TypeError:
                arguments = {}

            service = arguments.get("self")
            user = getattr(service, "current_user", None)
            username = getattr(user, "username", None)
            user_id = getattr(user, "user_id", None)
            payload = {
                "action": action_name.upper(),
                "username": username if isinstance(username, str) else None,
                "user_id": user_id if type(user_id) is int else None,
                "currency_code": _log_currency(arguments.get("currency_code")),
                "amount": _log_number(arguments.get("amount")),
                "rate": None,
                "base": None,
            }
            logger = logging.getLogger(ACTION_LOGGER_NAME)
            try:
                result = func(*args, **kwargs)
            except Exception as error:
                payload.update(
                    result="ERROR",
                    error_type=type(error).__name__,
                    error_message=_error_message(error),
                )
                logger.info(payload)
                raise

            if isinstance(result, dict):
                if isinstance(result.get("username"), str):
                    payload["username"] = result["username"]
                payload["currency_code"] = _log_currency(result.get("currency_code"))
                payload["amount"] = _log_number(result.get("amount"))
                payload["rate"] = _log_number(result.get("rate"))
                payload["base"] = _log_currency(result.get("base"))
                if verbose:
                    payload["before"] = _log_number(result.get("before"))
                    payload["after"] = _log_number(result.get("after"))
            payload["result"] = "OK"
            logger.info(payload)
            return result

        return wrapper

    return decorate
