"""Получение, объединение и обновление курсов валют."""

import logging
from datetime import UTC, datetime

from valutatrade_hub.core.exceptions import ApiRequestError, StorageError
from valutatrade_hub.core.utils import parse_timestamp, validate_number
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.parser_service.api_clients import (
    BaseApiClient,
    CoinGeckoClient,
    ExchangeRateApiClient,
)
from valutatrade_hub.parser_service.config import ParserConfig
from valutatrade_hub.parser_service.storage import RatesStorage, _pair_codes

LOGGER = logging.getLogger("valutatrade.parser")


class RatesUpdater:
    """Опросить независимые источники и передать корректные котировки хранилищу."""

    def __init__(
        self,
        clients: list[BaseApiClient] | None = None,
        storage: RatesStorage | None = None,
        config: ParserConfig | None = None,
    ) -> None:
        """Принимать подменяемые клиенты и хранилище без сетевых запросов."""
        if config is None:
            config = (
                storage.config
                if storage is not None
                else ParserConfig.from_settings(SettingsLoader())
            )
        self.config = config
        self.clients = (
            clients
            if clients is not None
            else [CoinGeckoClient(config), ExchangeRateApiClient(config)]
        )
        self.storage = storage if storage is not None else RatesStorage(config)

    @staticmethod
    def _quotes(client: BaseApiClient, values: dict, timestamp: str) -> dict:
        """Проверить весь ответ одного клиента перед объединением источников."""
        if not isinstance(values, dict) or not values:
            raise ApiRequestError(f"{client.source}: ответ не содержит курсов")
        quotes = {}
        try:
            for pair, value in values.items():
                _pair_codes(pair)
                metadata = dict(client.metadata.get(pair, {}))
                updated_at = metadata.pop("timestamp", timestamp)
                parse_timestamp(updated_at)
                quotes[pair] = {
                    "rate": validate_number(value, "Курс"),
                    "updated_at": updated_at,
                    "source": client.source,
                    "meta": metadata,
                }
        except (TypeError, ValueError):
            raise ApiRequestError(
                f"{client.source}: некорректные данные курса"
            ) from None
        return quotes

    def run_update(self, source: str | None = None) -> dict:
        """Продолжать обновление при отказе одного API, сохраняя остальные пары."""
        if source not in (None, "coingecko", "exchangerate"):
            raise ValueError("Источник должен быть coingecko или exchangerate")
        clients = [
            client
            for client in self.clients
            if source is None or client.source_key == source
        ]
        if not clients:
            raise ApiRequestError("Нет доступных клиентов выбранного источника")
        LOGGER.info("Starting rates update...")
        quotes, errors, sources = {}, [], []
        for client in clients:
            LOGGER.info("Fetching from %s...", client.source)
            try:
                values = client.fetch_rates()
                received = self._quotes(client, values, datetime.now(UTC).isoformat())
            except ApiRequestError as error:
                message = str(error)
                errors.append(message)
                LOGGER.error("Failed to fetch from %s: %s", client.source, message)
                continue
            for pair, quote in received.items():
                previous = quotes.get(pair)
                if previous is None or parse_timestamp(quote["updated_at"]) > (
                    parse_timestamp(previous["updated_at"])
                ):
                    quotes[pair] = quote
            sources.append(client.source)
            LOGGER.info("Fetching from %s... OK (%s rates)", client.source, len(values))
        if not sources:
            LOGGER.error("Rates update failed: all selected sources are unavailable")
            raise ApiRequestError("Не удалось обновить курсы: " + "; ".join(errors))
        LOGGER.info(
            "Writing %s rates to %s...", len(quotes), self.config.RATES_FILE_PATH
        )
        try:
            result = self.storage.save_rates(quotes, datetime.now(UTC).isoformat())
        except StorageError as error:
            LOGGER.error("Failed to write rates: %s", error)
            raise
        LOGGER.info(
            "Rates update completed: %s updated, %s errors",
            result["updated_count"],
            len(errors),
        )
        return {**result, "errors": errors, "sources": sources}
