"""Проверки API-клиентов, обновления и атомарного хранения курсов."""

import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import requests

from valutatrade_hub.core.exceptions import ApiRequestError, StorageError
from valutatrade_hub.infra.database import JsonStorage
from valutatrade_hub.parser_service.api_clients import (
    BaseApiClient,
    CoinGeckoClient,
    ExchangeRateApiClient,
)
from valutatrade_hub.parser_service.config import ParserConfig
from valutatrade_hub.parser_service.scheduler import main as scheduler_main
from valutatrade_hub.parser_service.storage import RatesStorage
from valutatrade_hub.parser_service.updater import RatesUpdater

TIMESTAMP = "2026-10-17T10:00:00Z"
NEWER_TIMESTAMP = "2026-10-17T11:00:00Z"
SECRET = "private-api-key"


def api_response(payload, status=200):
    """Создать ответ requests без сетевого запроса."""
    response = Mock(status_code=status, headers={"ETag": 'W/"snapshot"'})
    response.json.return_value = payload
    return response


def quote(rate, timestamp=TIMESTAMP, source="Fixture"):
    """Создать котировку, соответствующую контракту Parser-хранилища."""
    return {"rate": rate, "updated_at": timestamp, "source": source}


def fixture_client(source_key, values=None, error=None):
    """Создать независимый источник для проверки координации обновления."""
    client = Mock()
    client.source_key = source_key
    client.source = "CoinGecko" if source_key == "coingecko" else "ExchangeRate-API"
    client.metadata = {
        pair: {"timestamp": TIMESTAMP, "request_ms": 10, "status_code": 200}
        for pair in (values or {})
    }
    client.fetch_rates.return_value = values
    client.fetch_rates.side_effect = error
    return client


class ApiClientTests(unittest.TestCase):
    """Проверить контракт API и безопасное преобразование внешних ошибок."""

    def setUp(self):
        self.config = ParserConfig(EXCHANGERATE_API_KEY=SECRET)

    def test_config_reads_environment_at_construction_and_hides_key_in_repr(self):
        """Ключ не фиксируется при импорте и не попадает в repr конфигурации."""
        with patch.dict("os.environ", {"EXCHANGERATE_API_KEY": SECRET}):
            config = ParserConfig()
        self.assertEqual(config.EXCHANGERATE_API_KEY, SECRET)
        self.assertNotIn(SECRET, repr(config))
        with self.assertRaises(TypeError):
            BaseApiClient()
        for value in (0, -1, True, float("nan")):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                ParserConfig(REQUEST_TIMEOUT=value)

    def test_coingecko_uses_ids_and_returns_validated_numeric_pairs(self):
        """Несколько криптовалют запрашиваются вместе с timeout и нужной базой."""
        payload = {
            "bitcoin": {"usd": 59000},
            "ethereum": {"usd": 3700},
            "solana": {"usd": 145},
        }
        client = CoinGeckoClient(self.config)
        with patch(
            "valutatrade_hub.parser_service.api_clients.requests.get",
            return_value=api_response(payload),
        ) as get:
            self.assertEqual(
                client.fetch_rates(),
                {"BTC_USD": 59000, "ETH_USD": 3700, "SOL_USD": 145},
            )
        self.assertEqual(
            get.call_args.kwargs,
            {
                "params": {
                    "ids": "bitcoin,ethereum,solana",
                    "vs_currencies": "usd",
                },
                "timeout": 10,
            },
        )
        self.assertEqual(client.metadata["BTC_USD"]["raw_id"], "bitcoin")
        self.assertEqual(client.metadata["BTC_USD"]["status_code"], 200)
        self.assertEqual(client.metadata["BTC_USD"]["etag"], 'W/"snapshot"')

    def test_exchangerate_inverts_both_supported_response_formats(self):
        """Валют за доллар преобразуются в доллары за единицу валюты."""
        for field in ("rates", "conversion_rates"):
            with self.subTest(field=field):
                payload = {
                    "result": "success",
                    "base_code": "USD",
                    field: {"EUR": 0.8, "GBP": 0.5, "RUB": 100},
                    "time_last_update_utc": "Sat, 17 Oct 2026 10:00:00 +0000",
                }
                client = ExchangeRateApiClient(self.config)
                with patch(
                    "valutatrade_hub.parser_service.api_clients.requests.get",
                    return_value=api_response(payload),
                ) as get:
                    rates = client.fetch_rates()
                self.assertEqual(
                    rates, {"EUR_USD": 1.25, "GBP_USD": 2, "RUB_USD": 0.01}
                )
                self.assertIn(f"/{SECRET}/latest/USD", get.call_args.args[0])
                self.assertEqual(get.call_args.kwargs["timeout"], 10)
                self.assertEqual(
                    client.metadata["EUR_USD"]["api_updated_at"],
                    "2026-10-17T10:00:00+00:00",
                )
                self.assertNotEqual(
                    client.metadata["EUR_USD"]["timestamp"],
                    client.metadata["EUR_USD"]["api_updated_at"],
                )

    def test_missing_key_performs_no_request(self):
        """Отсутствие ключа не мешает использовать криптовалютный источник."""
        client = ExchangeRateApiClient(ParserConfig(EXCHANGERATE_API_KEY=None))
        with patch("valutatrade_hub.parser_service.api_clients.requests.get") as get:
            with self.assertRaisesRegex(ApiRequestError, "EXCHANGERATE_API_KEY"):
                client.fetch_rates()
        get.assert_not_called()

    def test_http_and_transport_failures_never_expose_key_or_url(self):
        """401/429/503 и сетевые ошибки превращаются в понятные ApiRequestError."""
        client = ExchangeRateApiClient(self.config)
        for status, expected in (
            (401, "API-ключ"),
            (429, "лимит"),
            (503, "недоступен"),
        ):
            with (
                self.subTest(status=status),
                patch(
                    "valutatrade_hub.parser_service.api_clients.requests.get",
                    return_value=api_response({}, status),
                ),
            ):
                with self.assertRaises(ApiRequestError) as caught:
                    client.fetch_rates()
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn(SECRET, str(caught.exception))
        for failure, expected in (
            (requests.exceptions.Timeout(SECRET), "время ожидания"),
            (requests.exceptions.ConnectionError(SECRET), "сети"),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                patch(
                    "valutatrade_hub.parser_service.api_clients.requests.get",
                    side_effect=failure,
                ),
            ):
                with self.assertRaises(ApiRequestError) as caught:
                    client.fetch_rates()
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn(SECRET, str(caught.exception))

    def test_error_payload_and_invalid_json_raise_domain_errors(self):
        """Ошибки JSON и отказ API не повторяют неизвестное содержимое ответа."""
        client = ExchangeRateApiClient(self.config)
        for payload in (
            [],
            {"result": "error", "error-type": "invalid-key"},
            {"result": "error", "error-type": SECRET},
            {"result": "error", "error-type": {"invalid": SECRET}},
            {
                "result": "success",
                "time_last_update_utc": 123,
                "rates": {"EUR": 0.8, "GBP": 0.5, "RUB": 100},
            },
            {"result": "success", "rates": {}},
        ):
            with (
                self.subTest(payload=payload),
                patch(
                    "valutatrade_hub.parser_service.api_clients.requests.get",
                    return_value=api_response(payload),
                ),
            ):
                with self.assertRaises(ApiRequestError) as caught:
                    client.fetch_rates()
                self.assertNotIn(SECRET, str(caught.exception))
        response = api_response({})
        response.json.side_effect = ValueError(SECRET)
        with (
            patch(
                "valutatrade_hub.parser_service.api_clients.requests.get",
                return_value=response,
            ),
            self.assertRaisesRegex(ApiRequestError, "JSON"),
        ):
            client.fetch_rates()

    def test_invalid_or_incomplete_numeric_response_rejects_entire_source(self):
        """Ноль, bool, NaN и пропущенные валюты не становятся котировками."""
        for value in (0, -1, True, float("nan"), "123", None):
            payload = {"bitcoin": {"usd": value}}
            with (
                self.subTest(value=value),
                patch(
                    "valutatrade_hub.parser_service.api_clients.requests.get",
                    return_value=api_response(payload),
                ),
            ):
                client = CoinGeckoClient(self.config)
                with self.assertRaises(ApiRequestError):
                    client.fetch_rates()
                self.assertEqual(client.metadata, {})
        for values in ({"EUR": 0.8}, {"EUR": 0, "GBP": 1, "RUB": 1}):
            with (
                self.subTest(fiat=values),
                patch(
                    "valutatrade_hub.parser_service.api_clients.requests.get",
                    return_value=api_response({"result": "success", "rates": values}),
                ),
                self.assertRaises(ApiRequestError),
            ):
                ExchangeRateApiClient(self.config).fetch_rates()


class _StorageCase(unittest.TestCase):
    """Проверить срезы, уникальные измерения и сохранность файлов при отказе."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = ParserConfig(
            RATES_FILE_PATH=self.root / "quotes.json",
            HISTORY_FILE_PATH=self.root / "measurements.json",
        )
        self.storage = JsonStorage(
            self.root, rates_file="quotes.json", history_file="measurements.json"
        )
        self.rates = RatesStorage(self.config, self.storage)

    def bytes(self):
        """Запомнить точные байты двух файлов для проверки отказов."""
        return tuple(
            path.read_bytes()
            for path in (self.config.RATES_FILE_PATH, self.config.HISTORY_FILE_PATH)
        )


class ParserStorageTests(_StorageCase):
    """Проверить срезы, уникальные измерения и атомарную запись."""

    def test_writes_snapshot_and_unique_utc_history_with_metadata(self):
        """Кэш содержит один срез пары, история — уникальные измерения с meta."""
        incoming = quote(59000, source="CoinGecko")
        incoming["meta"] = {"raw_id": "bitcoin", "request_ms": 20, "status_code": 200}
        result = self.rates.save_rates({"BTC_USD": incoming}, TIMESTAMP)
        self.assertEqual(result, {"updated_count": 1, "last_refresh": TIMESTAMP})
        snapshot = self.storage.load_rates()
        self.assertEqual(snapshot["pairs"]["BTC_USD"], quote(59000, source="CoinGecko"))
        record = self.rates.load_history()[0]
        self.assertEqual(record["id"], f"BTC_USD_{TIMESTAMP}")
        self.assertEqual(record["from_currency"], "BTC")
        self.assertEqual(record["to_currency"], "USD")
        self.assertEqual(record["meta"], incoming["meta"])
        before = self.bytes()
        repeated = self.rates.save_rates({"BTC_USD": incoming}, NEWER_TIMESTAMP)
        self.assertEqual(repeated["updated_count"], 0)
        self.assertEqual(self.bytes(), before)

    def test_older_or_equal_timestamp_cannot_overwrite_latest_snapshot(self):
        """Проигравшие срезы сохраняют байты файлов, победивший добавляет историю."""
        self.rates.save_rates({"BTC_USD": quote(60, NEWER_TIMESTAMP)}, NEWER_TIMESTAMP)
        cached_before = self.config.RATES_FILE_PATH.read_bytes()
        result = self.rates.save_rates({"BTC_USD": quote(1)}, NEWER_TIMESTAMP)
        self.assertEqual(result["updated_count"], 0)
        self.assertEqual(result["last_refresh"], NEWER_TIMESTAMP)
        self.assertEqual(self.config.RATES_FILE_PATH.read_bytes(), cached_before)
        self.assertEqual([row["rate"] for row in self.rates.load_history()], [60, 1])
        before = self.bytes()
        self.rates.save_rates({"BTC_USD": quote(2, NEWER_TIMESTAMP)}, NEWER_TIMESTAMP)
        self.assertEqual(self.bytes(), before)
        newest = "2026-10-17T12:00:00Z"
        self.rates.save_rates({"BTC_USD": quote(70, newest)}, newest)
        self.assertEqual(self.storage.load_rates()["pairs"]["BTC_USD"]["rate"], 70)
        self.assertEqual(len(self.rates.load_history()), 3)

    def test_whole_incoming_batch_is_validated_before_any_write(self):
        """Некорректная пара или число не приводят к частичному сохранению."""
        before = self.bytes()
        for invalid in ({"btc_USD": quote(1)}, {"EUR_USD": quote(float("nan"))}):
            with self.subTest(invalid=invalid), self.assertRaises(StorageError):
                self.rates.save_rates({"BTC_USD": quote(50), **invalid}, TIMESTAMP)
            self.assertEqual(self.bytes(), before)

    def test_corrupt_history_is_reported_without_replacing_either_file(self):
        """Повреждение истории видно пользователю и не обнуляет предыдущие данные."""
        self.config.HISTORY_FILE_PATH.write_text("{invalid", encoding="utf-8")
        before = self.bytes()
        with self.assertRaises(StorageError):
            self.rates.save_rates({"BTC_USD": quote(50)}, TIMESTAMP)
        self.assertEqual(self.bytes(), before)

    def test_failed_atomic_history_replace_preserves_both_files_and_cleans_tmp(self):
        """Отказ rename не оставляет частично записанный JSON и временные файлы."""
        before = self.bytes()
        with (
            patch(
                "valutatrade_hub.parser_service.storage.os.replace",
                side_effect=OSError("rename failed"),
            ),
            self.assertRaises(StorageError),
        ):
            self.rates.save_rates({"BTC_USD": quote(50)}, TIMESTAMP)
        self.assertEqual(self.bytes(), before)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_config_paths_follow_settings_and_storage_custom_names(self):
        """Parser и Core используют одни и те же настроенные файлы."""
        settings = Mock()
        settings.get.side_effect = {
            "data_dir": self.root,
            "rates_file": "quotes.json",
            "history_file": "measurements.json",
        }.__getitem__
        config = ParserConfig.from_settings(settings)
        self.assertEqual(config.RATES_FILE_PATH, self.config.RATES_FILE_PATH)
        self.assertEqual(config.HISTORY_FILE_PATH, self.config.HISTORY_FILE_PATH)
        inferred = RatesStorage(storage=self.storage)
        inferred.save_rates({"EUR_USD": quote(1.2)}, TIMESTAMP)
        self.assertEqual(inferred.history_path, self.config.HISTORY_FILE_PATH)
        self.assertEqual(len(inferred.load_history()), 1)


class RatesUpdaterTests(_StorageCase):
    """Проверить независимость источников и интеграцию с локальным кэшем."""

    def test_partial_failure_preserves_other_sources_and_reports_error(self):
        """Отказ криптоисточника не мешает фиатному и не удаляет прежний BTC."""
        self.rates.save_rates({"BTC_USD": quote(59000)}, TIMESTAMP)
        failed = fixture_client("coingecko", error=ApiRequestError("Network error"))
        success = fixture_client("exchangerate", {"EUR_USD": 1.2})
        updater = RatesUpdater([failed, success], self.rates, self.config)
        with self.assertLogs("valutatrade.parser", level="INFO") as logs:
            report = updater.run_update()
        self.assertEqual(report["updated_count"], 1)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["sources"], ["ExchangeRate-API"])
        self.assertIn("Starting rates update", "\n".join(logs.output))
        self.assertIn("Failed to fetch from CoinGecko", "\n".join(logs.output))
        pairs = self.storage.load_rates()["pairs"]
        self.assertEqual(pairs["BTC_USD"]["rate"], 59000)
        self.assertEqual(pairs["EUR_USD"]["rate"], 1.2)
        record = self.rates.load_history()[-1]
        self.assertEqual(record["source"], "ExchangeRate-API")
        self.assertEqual(record["meta"]["status_code"], 200)

    def test_complete_failure_does_not_touch_files(self):
        """Полный отказ возвращает ApiRequestError и сохраняет прежние байты."""
        clients = [
            fixture_client("coingecko", error=ApiRequestError("offline")),
            fixture_client("exchangerate", error=ApiRequestError("missing key")),
        ]
        before = self.bytes()
        updater = RatesUpdater(clients, self.rates, self.config)
        with self.assertLogs("valutatrade.parser", level="ERROR"):
            with self.assertRaises(ApiRequestError):
                updater.run_update()
        self.assertEqual(self.bytes(), before)

    def test_source_filter_does_not_call_unselected_client(self):
        """Ручной выбор источника не запрашивает другой API."""
        crypto = fixture_client("coingecko", {"BTC_USD": 59000})
        fiat = fixture_client("exchangerate", {"EUR_USD": 1.2})
        updater = RatesUpdater([crypto, fiat], self.rates, self.config)
        report = updater.run_update("coingecko")
        self.assertEqual(report["updated_count"], 1)
        fiat.fetch_rates.assert_not_called()
        with self.assertRaises(ValueError):
            updater.run_update("unknown")

    def test_malformed_client_response_does_not_stop_valid_source(self):
        """Ошибка контракта одного источника не загрязняет кэш и историю."""
        malformed = fixture_client("coingecko", {"BTC_USD": True})
        good = fixture_client("exchangerate", {"EUR_USD": 1.2})
        updater = RatesUpdater([malformed, good], self.rates, self.config)
        with self.assertLogs("valutatrade.parser", level="ERROR"):
            result = updater.run_update()
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(set(self.storage.load_rates()["pairs"]), {"EUR_USD"})

    def test_newer_source_wins_when_clients_return_same_pair(self):
        """При пересечении источников сохраняется более позднее измерение."""
        first = fixture_client("coingecko", {"BTC_USD": 60})
        second = fixture_client("exchangerate", {"BTC_USD": 70})
        second.metadata["BTC_USD"]["timestamp"] = NEWER_TIMESTAMP
        updater = RatesUpdater([first, second], self.rates, self.config)
        updater.run_update()
        self.assertEqual(self.storage.load_rates()["pairs"]["BTC_USD"]["rate"], 70)
        self.assertEqual(len(self.rates.load_history()), 1)


class SchedulerTests(unittest.TestCase):
    """Проверить запуск по таймеру и штатную остановку без реального ожидания."""

    def test_scheduler_runs_selected_source_and_stops_on_keyboard_interrupt(self):
        """Параметры доходят до updater; Ctrl+C завершает цикл с кодом 0."""
        result = {"updated_count": 3, "last_refresh": TIMESTAMP, "errors": []}
        with (
            patch(
                "sys.argv", ["scheduler", "--source", "coingecko", "--interval", "5"]
            ),
            patch("valutatrade_hub.parser_service.scheduler.SettingsLoader"),
            patch("valutatrade_hub.parser_service.scheduler.configure_logging"),
            patch("valutatrade_hub.parser_service.scheduler.ParserConfig") as config,
            patch("valutatrade_hub.parser_service.scheduler.DatabaseManager"),
            patch("valutatrade_hub.parser_service.scheduler.RatesStorage"),
            patch("valutatrade_hub.parser_service.scheduler.RatesUpdater") as updater,
            patch(
                "valutatrade_hub.parser_service.scheduler.time.sleep",
                side_effect=KeyboardInterrupt,
            ) as sleep,
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            config.from_settings.return_value.UPDATE_INTERVAL = 300
            updater.return_value.run_update.return_value = result
            self.assertEqual(scheduler_main(), 0)
        updater.return_value.run_update.assert_called_once_with("coingecko")
        sleep.assert_called_once_with(5)
        self.assertIn("остановлен", output.getvalue())


if __name__ == "__main__":
    unittest.main()
