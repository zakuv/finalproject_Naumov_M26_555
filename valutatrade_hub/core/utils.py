"""Проверка входных данных и расчёты по учебным курсам валют."""

import math
from datetime import UTC, datetime

from valutatrade_hub.core.exceptions import CurrencyNotFoundError

MIN_PASSWORD_LENGTH = 4
MIN_CURRENCY_CODE_LENGTH = 2
MAX_CURRENCY_CODE_LENGTH = 5
DEFAULT_BASE_CURRENCY = "USD"
RATES_TTL_SECONDS = 300
DEMO_SOURCE = "DemoStub"
SUPPORTED_CURRENCIES = frozenset({"USD", "EUR", "RUB", "BTC", "ETH"})
DEFAULT_EXCHANGE_RATES = {
    "EUR_USD": 1.0786,
    "BTC_USD": 59337.21,
    "RUB_USD": 0.01016,
    "ETH_USD": 3720.00,
}


def validate_user_id(value: int) -> int:
    """Проверить, что ID пользователя — положительное целое число без bool."""
    if type(value) is not int or value <= 0:
        raise ValueError(
            "Идентификатор пользователя должен быть положительным целым числом."
        )
    return value


def validate_username(value: str) -> str:
    """Вернуть имя без внешних пробелов, запретив пустое значение."""
    if not isinstance(value, str):
        raise ValueError("Имя пользователя должно быть строкой.")
    username = value.strip()
    if not username:
        raise ValueError("Имя пользователя не может быть пустым.")
    return username


def validate_password(value: str) -> str:
    """Проверить длину пароля и сохранить все переданные пробелы."""
    if not isinstance(value, str):
        raise ValueError("Пароль должен быть строкой.")
    if len(value) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов."
        )
    return value


def validate_currency_code(value: str) -> str:
    """Нормализовать код из 2–5 латинских букв в верхний регистр."""
    if not isinstance(value, str):
        raise ValueError("Код валюты должен быть строкой.")
    stripped = value.strip()
    code = stripped.upper()
    if (
        not MIN_CURRENCY_CODE_LENGTH <= len(code) <= MAX_CURRENCY_CODE_LENGTH
        or not stripped.isascii()
        or not all("A" <= character <= "Z" for character in code)
    ):
        raise ValueError(
            "Код валюты должен содержать "
            f"от {MIN_CURRENCY_CODE_LENGTH} до {MAX_CURRENCY_CODE_LENGTH} "
            "латинских букв."
        )
    return code


def validate_supported_currency(value: str) -> str:
    """Нормализовать код и проверить поддержку валюты приложением."""
    code = validate_currency_code(value)
    if code not in SUPPORTED_CURRENCIES:
        raise CurrencyNotFoundError(code)
    return code


def parse_timestamp(value: str) -> datetime:
    """Прочитать ISO-время в UTC; дату без часового пояса считать временем UTC."""
    message = "Время обновления должно быть корректной строкой ISO 8601."
    if not isinstance(value, str):
        raise ValueError(message)
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return timestamp.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ValueError(message) from None


def validate_number(
    value: int | float,
    field_name: str = "amount",
    *,
    allow_zero: bool = False,
) -> float:
    """Вернуть конечное положительное число либо неотрицательное при allow_zero."""
    description = "неотрицательным" if allow_zero else "положительным"
    message = f"'{field_name}' должен быть {description} конечным числом."
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(message) from None
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise ValueError(message)
    return number


def _lookup_exchange_rate(
    from_code: str,
    to_code: str,
    exchange_rates: dict[str, int | float],
) -> float | None:
    """Найти прямую или обратную пару, проверив используемую котировку."""
    if from_code == to_code:
        return 1.0
    direct_pair = f"{from_code}_{to_code}"
    if direct_pair in exchange_rates:
        return validate_number(exchange_rates[direct_pair], f"курс {direct_pair}")

    reverse_pair = f"{to_code}_{from_code}"
    if reverse_pair in exchange_rates:
        rate = validate_number(exchange_rates[reverse_pair], f"курс {reverse_pair}")
        return validate_number(1.0 / rate, f"курс {direct_pair}")
    return None


def get_exchange_rate(
    from_code: str,
    to_code: str,
    exchange_rates: dict[str, int | float] | None = None,
) -> float:
    """Получить прямой, обратный либо кросс-курс через USD без обращения к API."""
    source = validate_currency_code(from_code)
    target = validate_currency_code(to_code)
    if source == target:
        return 1.0

    rates = DEFAULT_EXCHANGE_RATES if exchange_rates is None else exchange_rates
    if not isinstance(rates, dict):
        raise ValueError("Курсы должны быть переданы словарём валютных пар.")

    direct_rate = _lookup_exchange_rate(source, target, rates)
    if direct_rate is not None:
        return direct_rate

    source_to_usd = _lookup_exchange_rate(source, "USD", rates)
    target_to_usd = _lookup_exchange_rate(target, "USD", rates)
    if source_to_usd is None or target_to_usd is None:
        raise ValueError(f"Курс {source}→{target} недоступен.")

    return validate_number(source_to_usd / target_to_usd, f"курс {source}_{target}")
