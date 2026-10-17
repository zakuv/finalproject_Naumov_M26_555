"""Конфигурация источников курсов и параметров обновления."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from valutatrade_hub.core.utils import validate_currency_code, validate_number

if TYPE_CHECKING:
    from valutatrade_hub.infra.settings import SettingsLoader


@dataclass
class ParserConfig:
    """Хранить параметры API; читать персональный ключ только из окружения."""

    EXCHANGERATE_API_KEY: str | None = field(
        default_factory=lambda: os.getenv("EXCHANGERATE_API_KEY"), repr=False
    )
    COINGECKO_URL: str = "https://api.coingecko.com/api/v3/simple/price"
    EXCHANGERATE_API_URL: str = "https://v6.exchangerate-api.com/v6"
    BASE_CURRENCY: str = "USD"
    FIAT_CURRENCIES: tuple[str, ...] = ("EUR", "GBP", "RUB")
    CRYPTO_CURRENCIES: tuple[str, ...] = ("BTC", "ETH", "SOL")
    CRYPTO_ID_MAP: dict[str, str] = field(
        default_factory=lambda: {
            "BTC": "bitcoin",
            "ETH": "ethereum",
            "SOL": "solana",
        }
    )
    RATES_FILE_PATH: str | Path = "data/rates.json"
    HISTORY_FILE_PATH: str | Path = "data/exchange_rates.json"
    REQUEST_TIMEOUT: float = 10
    UPDATE_INTERVAL: float = 300

    def __post_init__(self) -> None:
        """Проверить изменяемые параметры до первого запроса к API."""
        self.REQUEST_TIMEOUT = validate_number(self.REQUEST_TIMEOUT, "REQUEST_TIMEOUT")
        self.UPDATE_INTERVAL = validate_number(self.UPDATE_INTERVAL, "UPDATE_INTERVAL")
        self.BASE_CURRENCY = validate_currency_code(self.BASE_CURRENCY)
        self.FIAT_CURRENCIES = tuple(
            validate_currency_code(code) for code in self.FIAT_CURRENCIES
        )
        self.CRYPTO_CURRENCIES = tuple(
            validate_currency_code(code) for code in self.CRYPTO_CURRENCIES
        )
        for code in self.CRYPTO_CURRENCIES:
            if not isinstance(self.CRYPTO_ID_MAP.get(code), str) or not (
                self.CRYPTO_ID_MAP[code].strip()
            ):
                raise ValueError(f"Для {code} не задан ID CoinGecko")
        self.RATES_FILE_PATH = Path(self.RATES_FILE_PATH)
        self.HISTORY_FILE_PATH = Path(self.HISTORY_FILE_PATH)
        if self.RATES_FILE_PATH.resolve() == self.HISTORY_FILE_PATH.resolve():
            raise ValueError("Файлы кэша и истории должны различаться")

    @classmethod
    def from_settings(cls, settings: SettingsLoader) -> ParserConfig:
        """Использовать те же пути к данным, что и основной сервис."""
        data_dir = Path(settings.get("data_dir"))
        return cls(
            RATES_FILE_PATH=data_dir / settings.get("rates_file"),
            HISTORY_FILE_PATH=data_dir / settings.get("history_file"),
        )
