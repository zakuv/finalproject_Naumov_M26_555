"""Проверки команд Parser Service без сети и персонального API-ключа."""

import io
import json
import unittest
from contextlib import chdir, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from valutatrade_hub.cli.interface import main
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader


class ParserCliTests(unittest.TestCase):
    """Проверить обновление, фильтры, частичный отказ и восстановление REPL."""

    def setUp(self):
        """Изолировать файлы, настройки и синглтоны каждого запуска."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cwd = Path(temporary.name)
        for singleton in (SettingsLoader, DatabaseManager):
            previous = singleton._instance
            singleton._instance = None
            self.addCleanup(setattr, singleton, "_instance", previous)

    @staticmethod
    def response(url, **kwargs):
        """Вернуть ответы двух API в их исходных форматах."""
        response = Mock(status_code=200, headers={})
        if "coingecko" in url:
            response.json.return_value = {
                "bitcoin": {"usd": 59337.21},
                "ethereum": {"usd": 3720.0},
                "solana": {"usd": 145.12},
            }
        else:
            response.json.return_value = {
                "result": "success",
                "base_code": "USD",
                "conversion_rates": {
                    "USD": 1,
                    "EUR": 0.927,
                    "GBP": 0.809,
                    "RUB": 98.45,
                },
            }
        return response

    def run_commands(self, commands, response=None):
        """Выполнить настоящий цикл CLI с подставными HTTP-ответами."""
        output = io.StringIO()
        with (
            chdir(self.cwd),
            patch.dict("os.environ", {"EXCHANGERATE_API_KEY": "test-key"}),
            patch("builtins.input", side_effect=commands),
            patch("requests.get", side_effect=response or self.response) as request,
            redirect_stdout(output),
        ):
            self.assertEqual(main(), 0)
        return output.getvalue(), request

    def test_update_then_filter_convert_and_trade(self):
        """Все новые команды работают вместе с обычными сделками и кешем."""
        output, request = self.run_commands(
            [
                "update-rates",
                "show-rates --top 2",
                "show-rates --currency RUB",
                "show-rates --currency BTC --base EUR",
                "register --username alice --password 1234",
                "login --username alice --password 1234",
                "buy --currency BTC --amount 0.05",
                "sell --currency BTC --amount 0.01",
                "show-portfolio",
                "get-rate --from USD --to BTC",
                "exit",
            ]
        )
        self.assertEqual(request.call_count, 2)
        self.assertIn("Обновлено курсов: 6", output)
        self.assertIn("RUB_USD", output)
        self.assertIn("BTC_EUR", output)
        self.assertIn("Покупка выполнена", output)
        self.assertIn("Продажа выполнена", output)
        self.assertNotIn("Traceback", output)
        history = json.loads((self.cwd / "data/exchange_rates.json").read_text())
        self.assertEqual(len(history), 6)

    def test_one_failed_source_keeps_successful_update_and_reports_errors(self):
        """Отказ API фиатных валют оставляет полученные криптокурсы доступными."""

        def response(url, **kwargs):
            if "coingecko" in url:
                return self.response(url, **kwargs)
            return Mock(status_code=401, headers={})

        output, request = self.run_commands(
            [
                "update-rates",
                "show-rates --currency BTC",
                "exit",
            ],
            response,
        )
        self.assertEqual(request.call_count, 2)
        self.assertIn("Обновлено курсов: 3", output)
        self.assertIn("с ошибками", output)
        self.assertIn("BTC_USD", output)
        self.assertNotIn("test-key", output)

    def test_invalid_commands_and_empty_cache_never_request_network(self):
        """Ошибки фильтров и источника не прерывают интерактивный цикл."""
        output, request = self.run_commands(
            [
                "show-rates",
                "show-rates --top 0",
                "show-rates --currency ABC",
                "update-rates --source unknown",
                "help show-rates",
                "exit",
            ]
        )
        request.assert_not_called()
        self.assertIn("кеш", output.lower().replace("кэш", "кеш"))
        self.assertIn("Неверные аргументы", output)
        self.assertIn("show-rates [--currency", output)
        self.assertNotIn("Traceback", output)
