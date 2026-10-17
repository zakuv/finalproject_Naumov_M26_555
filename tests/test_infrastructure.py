"""Проверки валют, конфигурации и журналов без общей сессии между тестами."""

import json
import os
import subprocess
import sys
import textwrap
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from valutatrade_hub.core.currencies import (
    CryptoCurrency,
    Currency,
    FiatCurrency,
    get_currency,
    get_supported_codes,
)
from valutatrade_hub.core.exceptions import CurrencyNotFoundError
from valutatrade_hub.core.utils import validate_supported_currency

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAIN_SCRIPT = PROJECT_ROOT / "main.py"


class InfrastructureTests(unittest.TestCase):
    """Изолировать синглтоны и файловые обработчики в дочерних процессах."""

    def setUp(self):
        """Выделить каталог для конфигурации, JSON и логов одной проверки."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cwd = Path(temporary.name)

    def write_config(self, **overrides):
        """Записать конфигурацию с нестандартными путями и именами файлов."""
        settings = {
            "data_dir": "custom-data",
            "users_file": "accounts.json",
            "portfolios_file": "wallets.json",
            "rates_file": "quotes.json",
            "history_file": "history.json",
            "rates_ttl_seconds": 900,
            "default_base_currency": "EUR",
            "log_file": "custom-logs/operations.log",
        }
        settings.update(overrides)
        config = self.cwd / "pyproject.toml"
        lines = ["[tool.valutatrade]"]
        lines.extend(f"{key} = {json.dumps(value)}" for key, value in settings.items())
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return config

    def run_python(self, source):
        """Выполнить сценарий с отдельными экземплярами синглтонов."""
        result = subprocess.run(
            [sys.executable, "-B", "-c", textwrap.dedent(source)],
            cwd=self.cwd,
            env=os.environ | {"PYTHONPATH": str(PROJECT_ROOT)},
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return result

    def run_cli(self, commands, *, status=0):
        """Передать команды настоящему приложению и проверить завершение."""
        result = subprocess.run(
            [sys.executable, "-B", str(MAIN_SCRIPT)],
            cwd=self.cwd,
            input=commands,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, status, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        if status == 0:
            self.assertEqual(result.stderr, "")
        return result

    def test_currency_hierarchy_factory_and_expected_display(self):
        """Общий интерфейс остаётся абстрактным, фабрика создаёт нужные типы."""
        with self.assertRaises(TypeError):
            Currency("Dollar", "USD")
        self.assertEqual(
            get_supported_codes(), ("BTC", "ETH", "EUR", "GBP", "RUB", "SOL", "USD")
        )
        dollar = get_currency(" usd ")
        self.assertIsInstance(dollar, FiatCurrency)
        self.assertEqual(
            dollar.get_display_info(),
            "[FIAT] USD — US Dollar (Issuing: United States)",
        )
        self.assertIsNot(dollar, get_currency("USD"))
        bitcoin = CryptoCurrency("Bitcoin", "BTC", "SHA-256", 1.12e12)
        self.assertEqual(
            bitcoin.get_display_info(),
            "[CRYPTO] BTC — Bitcoin (Algo: SHA-256, MCAP: 1.12e12)",
        )
        self.assertIsInstance(get_currency("ETH"), CryptoCurrency)
        self.assertIsInstance(get_currency("SOL"), CryptoCurrency)
        self.assertIsInstance(get_currency("GBP"), FiatCurrency)
        self.assertEqual(validate_supported_currency(" eur "), "EUR")

    def test_currency_validation_rejects_bad_metadata_and_unknown_codes(self):
        """Некорректные коды и неизвестные валюты дают разные предметные ошибки."""
        for code in ("ABC", "ZZZ"):
            with self.subTest(code=code), self.assertRaises(CurrencyNotFoundError):
                get_currency(code)
        for code in ("", "A", "USD1", "РУБ", None):
            with self.subTest(code=code), self.assertRaises(ValueError):
                get_currency(code)
        for field in ("", "  ", None):
            with self.subTest(field=field), self.assertRaises(ValueError):
                FiatCurrency(field, "USD", "United States")
            with self.subTest(country=field), self.assertRaises(ValueError):
                FiatCurrency("US Dollar", "USD", field)
        for cap in (-1, True, float("nan"), float("inf"), "100"):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                CryptoCurrency("Bitcoin", "BTC", "SHA-256", cap)

    def test_imports_are_lazy_and_missing_default_config_uses_defaults(self):
        """Импорты не создают файлы и экземпляры, запуск без TOML допустим."""
        self.run_python("""
            from pathlib import Path
            from valutatrade_hub.infra.database import DatabaseManager
            from valutatrade_hub.infra.settings import SettingsLoader
            from valutatrade_hub.logging_config import configure_logging
            assert SettingsLoader._instance is None
            assert DatabaseManager._instance is None
            assert list(Path.cwd().iterdir()) == []
            settings = SettingsLoader()
            assert settings is SettingsLoader()
            assert settings.get('data_dir') == Path.cwd() / 'data'
            assert settings.get('rates_ttl_seconds') == 300
            assert settings.get('missing', 'fallback') == 'fallback'
            assert list(Path.cwd().iterdir()) == []
        """)

    def test_settings_cache_reload_and_failed_reload_preserve_valid_state(self):
        """Файл читается повторно только по reload; ошибка не портит кеш."""
        self.write_config()
        self.run_python("""
            from pathlib import Path
            from valutatrade_hub.core.exceptions import ConfigurationError
            from valutatrade_hub.infra.settings import SettingsLoader
            settings = SettingsLoader('pyproject.toml')
            assert settings.get('DATA_DIR') == Path.cwd() / 'custom-data'
            assert settings.get('log_file') == Path.cwd() / 'custom-logs/operations.log'
            config = Path('pyproject.toml')
            text = config.read_text()
            config.write_text(text.replace('900', '1200'))
            assert SettingsLoader() is settings
            assert settings.get('rates_ttl_seconds') == 900
            settings.reload()
            assert settings.get('rates_ttl_seconds') == 1200
            config.write_text('[tool.valutatrade]\\nrates_ttl_seconds = true\\n')
            try:
                settings.reload()
            except ConfigurationError:
                pass
            else:
                raise AssertionError('Invalid TTL accepted')
            assert settings.get('rates_ttl_seconds') == 1200
            try:
                settings.reload('missing.toml')
            except ConfigurationError:
                pass
            else:
                raise AssertionError('Missing explicit configuration accepted')
            assert settings.get('rates_ttl_seconds') == 1200
        """)

    def test_invalid_configuration_values_raise_configuration_error(self):
        """Неверные типы и пересекающиеся имена отклоняются до смены настроек."""
        self.write_config()
        self.run_python("""
            from pathlib import Path
            from valutatrade_hub.core.exceptions import ConfigurationError
            from valutatrade_hub.infra.settings import SettingsLoader
            settings = SettingsLoader()
            invalid = (
                'log_level = []',
                'rates_ttl_seconds = -1',
                'log_max_bytes = true',
                'log_backup_count = -1',
                'default_base_currency = "ZZZ"',
                'users_file = "../users.json"',
                'users_file = "same.json"\\nportfolios_file = "SAME.json"',
            )
            for value in invalid:
                Path('pyproject.toml').write_text('[tool.valutatrade]\\n' + value)
                try:
                    settings.reload()
                except ConfigurationError:
                    pass
                else:
                    raise AssertionError('Invalid configuration accepted: ' + value)
                assert settings.get('rates_ttl_seconds') == 900
        """)

    def test_database_singleton_uses_all_configured_filenames(self):
        """Чтение и атомарная запись используют настроенные имена всех JSON."""
        self.write_config()
        self.run_python("""
            import json
            from pathlib import Path
            from valutatrade_hub.core.models import Portfolio, User
            from valutatrade_hub.infra.database import DatabaseManager, JsonStorage
            from valutatrade_hub.infra.settings import SettingsLoader
            storage = DatabaseManager(SettingsLoader())
            assert DatabaseManager() is storage
            assert DatabaseManager(SettingsLoader()) is storage
            user = User.create(1, 'Alice', 'private-password')
            storage.save_users([user])
            portfolio = Portfolio(user)
            portfolio.add_currency('USD').deposit(4)
            storage.save_portfolios([portfolio])
            assert storage.load_users()[0].username == 'Alice'
            assert storage.load_portfolios()[0].get_wallet('USD').balance == 4
            rates = {'pairs': {}, 'last_refresh': None}
            storage.save_rates(rates)
            assert storage.load_rates() == rates
            directory = Path('custom-data')
            assert {path.name for path in directory.iterdir()} == {
                'accounts.json', 'wallets.json', 'quotes.json', 'history.json'
            }
            assert json.loads((directory / 'history.json').read_text()) == []
            separate = JsonStorage('independent')
            assert separate is not JsonStorage('another')
            assert separate.load_users() == []
            assert not Path('data').exists()
        """)

    def test_log_path_collision_cannot_modify_data_or_configuration(self):
        """Коллизии путей отклоняются до открытия журнала и создания JSON."""
        for log_file in ("data/users.json", "data/USERS.json", "pyproject.toml"):
            with self.subTest(log_file=log_file):
                config = self.write_config(
                    data_dir="data", users_file="users.json", log_file=log_file
                )
                before = config.read_bytes()
                self.run_python("""
                    from valutatrade_hub.core.exceptions import ConfigurationError
                    from valutatrade_hub.infra.settings import SettingsLoader
                    try:
                        SettingsLoader('pyproject.toml')
                    except ConfigurationError:
                        pass
                    else:
                        raise AssertionError('Conflicting log path accepted')
                """)
                self.assertEqual(config.read_bytes(), before)
                self.assertEqual(list(self.cwd.iterdir()), [config])

    def test_decorator_preserves_return_and_original_exception(self):
        """Обёртка возвращает исходный объект и пробрасывает то же исключение."""
        self.write_config()
        self.run_python("""
            import json
            from pathlib import Path
            from valutatrade_hub.decorators import log_action
            from valutatrade_hub.infra.settings import SettingsLoader
            from valutatrade_hub.logging_config import configure_logging
            logger = configure_logging(SettingsLoader())
            report = {
                'username': 'Alice', 'currency_code': 'EUR', 'amount': 2,
                'rate': 1.5, 'base': 'USD', 'before': 0, 'after': 2,
            }
            @log_action('BUY', verbose=True)
            def buy(currency_code, amount, *, password):
                return report
            assert buy('EUR', 2, password='never-log-this') is report
            failure = ValueError('Expected validation failure')
            @log_action('SELL')
            def sell(currency_code, amount, *, password):
                raise failure
            try:
                sell('EUR', float('nan'), password='never-log-this')
            except ValueError as caught:
                assert caught is failure
            else:
                raise AssertionError('Decorator swallowed an exception')
            for handler in logger.handlers:
                handler.flush()
            text = Path('custom-logs/operations.log').read_text()
            records = [json.loads(line) for line in text.splitlines()]
            assert [record['result'] for record in records] == ['OK', 'ERROR']
            assert records[0]['before'] == 0 and records[0]['after'] == 2
            assert records[1]['error_type'] == 'ValueError'
            assert records[1]['amount'] is None
            assert 'never-log-this' not in text
        """)

    def test_logging_setup_is_idempotent_and_rotates_valid_json(self):
        """Повторная настройка не дублирует записи; ротация ограничивает архивы."""
        self.write_config(log_max_bytes=500, log_backup_count=2)
        self.run_python("""
            import json
            from pathlib import Path
            from logging.handlers import RotatingFileHandler
            from valutatrade_hub.infra.settings import SettingsLoader
            from valutatrade_hub.logging_config import configure_logging
            settings = SettingsLoader()
            logger = configure_logging(settings)
            assert configure_logging(settings) is logger
            assert len([
                handler for handler in logger.handlers
                if isinstance(handler, RotatingFileHandler)
            ]) == 1
            logger.info({'action': 'PROBE', 'result': 'OK'})
            path = Path('custom-logs/operations.log')
            assert len(path.read_text().splitlines()) == 1
            for index in range(20):
                logger.info({'action': 'PROBE', 'result': 'OK', 'sequence': index})
            paths = list(path.parent.glob('operations.log*'))
            assert len(paths) == 3
            for file in paths:
                for line in file.read_text().splitlines():
                    record = json.loads(line)
                    assert record['timestamp'] and record['level'] == 'INFO'
        """)

    def test_cli_honors_configured_base_and_logs_trade_results_without_secrets(self):
        """База берётся из TOML, журнал отражает успех и отказ без пароля и хеша."""
        self.write_config()
        self.run_cli("exit\n")
        timestamp = datetime.now(UTC).isoformat()
        quotes = {
            "pairs": {
                "EUR_USD": {"rate": 2, "updated_at": timestamp, "source": "Fixture"}
            },
            "last_refresh": timestamp,
        }
        (self.cwd / "custom-data/quotes.json").write_text(
            json.dumps(quotes), encoding="utf-8"
        )
        password = "distinct-private-password"
        result = self.run_cli(
            f"register --username Alice --password {password}\n"
            f"login --username Alice --password {password}\n"
            "buy --currency EUR --amount 2\n"
            "sell --currency EUR --amount 3\n"
            "buy --currency BTC --amount nan\n"
            "show-portfolio\n"
            "exit\n"
        )
        self.assertIn("(база: EUR)", result.stdout)
        self.assertIn("ИТОГО: 2.00 EUR", result.stdout)
        self.assertIn("Недостаточно средств", result.stdout)
        raw = (self.cwd / "custom-logs/operations.log").read_text("utf-8")
        records = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(
            [entry["result"] for entry in records], ["OK", "ERROR", "ERROR"]
        )
        self.assertEqual([entry["action"] for entry in records], ["BUY", "SELL", "BUY"])
        for entry in records:
            self.assertEqual(entry["username"], "Alice")
            self.assertTrue(datetime.fromisoformat(entry["timestamp"]).tzinfo)
        self.assertEqual(records[0]["base"], "USD")
        self.assertEqual(records[1]["error_type"], "InsufficientFundsError")
        self.assertIsNone(records[2]["amount"])
        account = json.loads((self.cwd / "custom-data/accounts.json").read_text())[0]
        for secret in (password, account["hashed_password"], account["salt"]):
            self.assertNotIn(secret, raw + result.stdout + result.stderr)
        self.assertNotIn('"hashed_password"', raw)
        self.assertNotIn('"salt"', raw)

    def test_cli_uses_configured_ttl_for_cached_quotes(self):
        """Одна и та же котировка сохраняется или обновляется согласно TTL."""
        self.write_config(rates_ttl_seconds=900)
        self.run_cli("exit\n")
        timestamp = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        quotes = {
            "pairs": {
                "EUR_USD": {"rate": 9, "updated_at": timestamp, "source": "Fixture"}
            },
            "last_refresh": timestamp,
        }
        path = self.cwd / "custom-data/quotes.json"
        path.write_text(json.dumps(quotes), encoding="utf-8")
        before = path.read_bytes()
        fresh = self.run_cli("get-rate --from EUR --to USD\nexit\n")
        self.assertIn("Курс EUR→USD: 9 ", fresh.stdout)
        self.assertIn("Источник: Fixture", fresh.stdout)
        self.assertEqual(path.read_bytes(), before)
        self.write_config(rates_ttl_seconds=1)
        self.run_python("""
            from unittest.mock import Mock
            from valutatrade_hub.core.usecases import RateService
            provider = Mock(return_value={'EUR_USD': 1.25})
            rates = RateService(provider=provider, source='FixtureRefresh')
            quote = rates.get_rate('EUR', 'USD')
            assert quote['rate'] == 1.25
            assert quote['source'] == 'FixtureRefresh'
            provider.assert_called_once()
        """)
        updated = json.loads(path.read_text())
        self.assertEqual(updated["pairs"]["EUR_USD"]["rate"], 1.25)
        self.assertNotEqual(path.read_bytes(), before)

    def test_cli_reports_bad_configuration_before_creating_runtime_files(self):
        """Ошибка настроек завершает запуск с сообщением без traceback."""
        self.write_config(rates_ttl_seconds=False)
        result = self.run_cli("exit\n", status=1)
        self.assertIn("rates_ttl_seconds", result.stderr)
        self.assertFalse((self.cwd / "custom-data").exists())
        self.assertFalse((self.cwd / "custom-logs").exists())


if __name__ == "__main__":
    unittest.main()
