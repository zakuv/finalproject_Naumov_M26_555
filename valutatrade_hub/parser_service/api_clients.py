"""Клиенты внешних API фиатных валют и криптовалют."""

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import perf_counter

import requests

from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.utils import validate_number
from valutatrade_hub.parser_service.config import ParserConfig


class BaseApiClient(ABC):
    """Получать числовые пары и отдельно сохранять метаданные последнего ответа."""

    source: str
    source_key: str

    def __init__(self, config: ParserConfig | None = None) -> None:
        """Запомнить общую конфигурацию без выполнения сетевых запросов."""
        self.config = config if config is not None else ParserConfig()
        self.metadata: dict[str, dict] = {}

    def _request(self, url: str, params: dict | None = None) -> tuple[dict, dict]:
        """Проверить HTTP и JSON, не включая URL с ключом в ошибки."""
        started = perf_counter()
        try:
            response = requests.get(
                url, params=params, timeout=self.config.REQUEST_TIMEOUT
            )
        except requests.exceptions.Timeout:
            raise ApiRequestError(f"{self.source}: превышено время ожидания") from None
        except requests.exceptions.RequestException:
            raise ApiRequestError(f"{self.source}: ошибка сети") from None
        if response.status_code != 200:
            reason = {
                401: "неверный API-ключ или нет доступа",
                403: "доступ запрещён",
                429: "превышен лимит запросов",
            }.get(response.status_code, "сервис недоступен")
            raise ApiRequestError(
                f"{self.source}: HTTP {response.status_code}, {reason}"
            )
        try:
            payload = response.json()
        except (ValueError, requests.exceptions.RequestException):
            raise ApiRequestError(f"{self.source}: некорректный JSON") from None
        if not isinstance(payload, dict):
            raise ApiRequestError(f"{self.source}: ожидается JSON-объект")
        metadata = {
            "timestamp": datetime.now(UTC).isoformat(),
            "request_ms": round((perf_counter() - started) * 1000, 3),
            "status_code": response.status_code,
        }
        etag = response.headers.get("ETag")
        if etag:
            metadata["etag"] = etag
        return payload, metadata

    @abstractmethod
    def fetch_rates(self) -> dict[str, float]:
        """Вернуть пары вида BTC_USD и положительные конечные курсы."""


class CoinGeckoClient(BaseApiClient):
    """Загружать цены криптовалют по ID из CRYPTO_ID_MAP."""

    source = "CoinGecko"
    source_key = "coingecko"

    def fetch_rates(self) -> dict[str, float]:
        """Привести simple/price к единому формату пар валют."""
        config = self.config
        ids = [config.CRYPTO_ID_MAP[code] for code in config.CRYPTO_CURRENCIES]
        payload, common = self._request(
            config.COINGECKO_URL,
            {"ids": ",".join(ids), "vs_currencies": config.BASE_CURRENCY.lower()},
        )
        rates, metadata = {}, {}
        try:
            for code in config.CRYPTO_CURRENCIES:
                raw_id = config.CRYPTO_ID_MAP[code]
                rate = validate_number(
                    payload[raw_id][config.BASE_CURRENCY.lower()], "Курс"
                )
                pair = f"{code}_{config.BASE_CURRENCY}"
                rates[pair] = rate
                metadata[pair] = {**common, "raw_id": raw_id}
        except (KeyError, TypeError, ValueError):
            raise ApiRequestError(f"{self.source}: некорректные данные курса") from None
        if not rates:
            raise ApiRequestError(f"{self.source}: ответ не содержит курсов")
        self.metadata = metadata
        return rates


class ExchangeRateApiClient(BaseApiClient):
    """Загружать фиатные курсы и приводить их к направлению валюта → база."""

    source = "ExchangeRate-API"
    source_key = "exchangerate"

    def fetch_rates(self) -> dict[str, float]:
        """Инвертировать котировки базы: EUR за USD → USD за EUR."""
        config = self.config
        key = config.EXCHANGERATE_API_KEY
        if not isinstance(key, str) or not key.strip():
            raise ApiRequestError(
                f"{self.source}: задайте EXCHANGERATE_API_KEY в окружении"
            )
        url = (
            f"{config.EXCHANGERATE_API_URL.rstrip('/')}/"
            f"{key.strip()}/latest/{config.BASE_CURRENCY}"
        )
        payload, common = self._request(url)
        if payload.get("result") != "success":
            reason = {
                "invalid-key": "неверный API-ключ",
                "inactive-account": "учётная запись не активирована",
                "quota-reached": "превышен лимит запросов",
                "unsupported-code": "неподдерживаемая базовая валюта",
            }.get(
                payload.get("error-type")
                if isinstance(payload.get("error-type"), str)
                else "",
                "сервис отклонил запрос",
            )
            raise ApiRequestError(f"{self.source}: {reason}")
        rates, metadata = {}, {}
        try:
            if payload.get("base_code", config.BASE_CURRENCY) != config.BASE_CURRENCY:
                raise ValueError("Неверная база ответа")
            values = payload.get("conversion_rates", payload.get("rates"))
            if not isinstance(values, dict):
                raise ValueError("Отсутствуют курсы")
            timestamp = common["timestamp"]
            if "time_last_update_unix" in payload:
                seconds = validate_number(payload["time_last_update_unix"], "Время")
                timestamp = datetime.fromtimestamp(seconds, UTC).isoformat()
            elif "time_last_update_utc" in payload:
                update_time = parsedate_to_datetime(payload["time_last_update_utc"])
                if update_time.tzinfo is None:
                    update_time = update_time.replace(tzinfo=UTC)
                timestamp = update_time.astimezone(UTC).isoformat()
            for code in config.FIAT_CURRENCIES:
                rate = validate_number(1.0 / validate_number(values[code], "Курс"))
                pair = f"{code}_{config.BASE_CURRENCY}"
                rates[pair] = rate
                metadata[pair] = {**common, "api_updated_at": timestamp}
        except (
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
            OSError,
            AttributeError,
        ):
            raise ApiRequestError(f"{self.source}: некорректные данные курса") from None
        if not rates:
            raise ApiRequestError(f"{self.source}: ответ не содержит курсов")
        self.metadata = metadata
        return rates
