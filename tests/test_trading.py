"""Проверки торговли, свежести курсов и сохранности портфелей."""

import json
import subprocess
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    CurrencyNotFoundError,
    InsufficientFundsError,
    StorageError,
)
from valutatrade_hub.core.usecases import PortfolioService, RateService, UserService
from valutatrade_hub.infra.database import JsonStorage

MAIN_SCRIPT = Path(__file__).resolve().parents[1] / "main.py"


class TradingTests(unittest.TestCase):
    """Проверить сценарии на настоящем JSON без внешних API."""

    def setUp(self):
        """Создать независимые данные и управляемый источник котировок."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cwd = Path(temporary.name)
        self.storage = JsonStorage(self.cwd / "data")
        self.users = UserService(self.storage)
        self.now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
        self.provider = Mock(
            return_value={
                "EUR_USD": 2.0,
                "BTC_USD": 100.0,
                "ETH_USD": 10.0,
                "RUB_USD": 0.01,
            }
        )
        self.rates = RateService(
            self.storage,
            provider=self.provider,
            clock=lambda: self.now,
            source="Fixture",
        )
        self.trading = PortfolioService(self.storage, self.users, self.rates)

    def login_alice(self):
        """Зарегистрировать пользователя без начальных денег и войти."""
        self.users.register("Alice", "alice-password")
        return self.users.login("Alice", "alice-password")

    def portfolio_bytes(self):
        """Прочитать сохранённые портфели без нормализации JSON."""
        return (self.storage.data_dir / "portfolios.json").read_bytes()

    def store_quotes(self, quotes):
        """Сохранить котировки с индивидуальными датами обновления."""
        self.storage.save_rates(
            {
                "pairs": {
                    pair: {
                        "rate": rate,
                        "updated_at": updated_at.isoformat(),
                        "source": "Fixture",
                    }
                    for pair, rate, updated_at in quotes
                },
                "last_refresh": self.now.isoformat(),
            }
        )

    def test_trade_and_portfolio_operations_require_login(self):
        """Анонимные операции не создают кошельки и не запрашивают курсы."""
        before = self.portfolio_bytes()
        actions = [
            lambda: self.trading.show_portfolio(),
            lambda: self.trading.buy("EUR", 1),
            lambda: self.trading.sell("EUR", 1),
        ]
        for action in actions:
            with self.subTest(action=action):
                with self.assertRaises(PermissionError):
                    action()
        self.assertEqual(self.portfolio_bytes(), before)
        self.provider.assert_not_called()

    def test_buy_sell_and_valuation_use_usd_as_estimate_without_seed_money(self):
        """Сделка меняет только выбранную валюту, а USD используется для оценки."""
        self.login_alice()
        self.assertEqual(self.trading.show_portfolio()["rows"], [])
        bought = self.trading.buy(" eur ", 5)
        self.assertEqual(bought["username"], "Alice")
        self.assertEqual(bought["currency_code"], "EUR")
        self.assertEqual(bought["base"], "USD")
        self.assertEqual(
            (bought["amount"], bought["before"], bought["after"]), (5, 0, 5)
        )
        self.assertEqual((bought["rate"], bought["value"]), (2, 10))
        self.assertEqual(bought["source"], "Fixture")
        self.assertEqual(datetime.fromisoformat(bought["updated_at"]), self.now)
        wallet_codes = set(self.storage.load_portfolios()[0].wallets)
        self.assertEqual(wallet_codes, {"EUR"})

        sold = self.trading.sell("EUR", 2)
        self.assertEqual((sold["before"], sold["after"], sold["value"]), (5, 3, 4))
        self.assertEqual(set(self.storage.load_portfolios()[0].wallets), {"EUR"})
        self.trading.buy("USD", 10)
        self.trading.sell("USD", 4)
        in_usd = self.trading.show_portfolio()
        rows = {row["currency_code"]: row for row in in_usd["rows"]}
        self.assertEqual(in_usd["username"], "Alice")
        self.assertEqual(in_usd["base"], "USD")
        self.assertEqual((rows["USD"]["balance"], rows["EUR"]["balance"]), (6, 3))
        self.assertEqual(in_usd["total"], 12)
        self.assertEqual(self.trading.show_portfolio("EUR")["total"], 6)

    def test_two_users_remain_independent_across_restart_and_further_trades(self):
        """Повторное чтение сохраняет балансы и не меняет чужой портфель."""
        self.login_alice()
        self.users.register("Bob", "bob-password")
        self.trading.buy("EUR", 5)
        self.users.login("Bob", "bob-password")
        self.trading.buy("BTC", 0.02)
        by_id = {item.user_id: item for item in self.storage.load_portfolios()}
        bob_id = self.users.current_user.user_id
        bob_before = by_id[bob_id].to_dict()

        restarted = JsonStorage(self.storage.data_dir)
        users = UserService(restarted)
        self.assertIsNone(users.current_user)
        users.login("Alice", "alice-password")
        rates = RateService(
            restarted,
            provider=self.provider,
            clock=lambda: self.now,
            source="Fixture",
        )
        trading = PortfolioService(restarted, users, rates)
        trading.sell("EUR", 1)
        trading.buy("ETH", 2)

        final = {item.user_id: item for item in restarted.load_portfolios()}
        self.assertEqual(final[bob_id].to_dict(), bob_before)
        alice = final[users.current_user.user_id]
        self.assertEqual(alice.get_wallet("EUR").balance, 4)
        self.assertEqual(alice.get_wallet("ETH").balance, 2)

    def test_invalid_trades_and_insufficient_funds_do_not_change_portfolios(self):
        """Отказы не создают лишний кошелёк и не меняют сохранённый баланс."""
        self.login_alice()
        self.trading.buy("EUR", 2)
        before = self.portfolio_bytes()
        for amount in (0, -1, True, float("nan")):
            for operation in (self.trading.buy, self.trading.sell):
                with self.subTest(operation=operation.__name__, amount=amount):
                    with self.assertRaises(ValueError):
                        operation("BTC", amount)
                    self.assertEqual(self.portfolio_bytes(), before)
        with self.assertRaises(CurrencyNotFoundError):
            self.trading.buy("ZZZ", 1)
        with self.assertRaises(InsufficientFundsError) as caught:
            self.trading.sell("EUR", 3)
        self.assertEqual(
            (caught.exception.available, caught.exception.required), (2, 3)
        )
        self.assertEqual(self.portfolio_bytes(), before)

    def test_provider_and_save_failures_leave_existing_portfolios_unchanged(self):
        """Отказ котировки или записи не сохраняет сделку и новый кошелёк."""
        self.login_alice()
        self.trading.buy("EUR", 1)
        before = self.portfolio_bytes()
        self.now += timedelta(minutes=10)
        self.provider.side_effect = RuntimeError("source unavailable")
        with self.assertRaises(ApiRequestError):
            self.trading.buy("BTC", 1)
        self.assertEqual(self.portfolio_bytes(), before)
        self.provider.side_effect = None
        failure = StorageError(
            self.storage.data_dir / "portfolios.json", "write failed"
        )
        with patch.object(self.storage, "save_portfolios", side_effect=failure):
            with self.assertRaises(StorageError):
                self.trading.buy("BTC", 1)
        self.assertEqual(self.portfolio_bytes(), before)
        restored = self.storage.load_portfolios()[0]
        self.assertEqual(set(restored.wallets), {"EUR"})
        self.assertEqual(restored.get_wallet("EUR").balance, 1)

    def test_fresh_direct_inverse_cross_and_identity_rates_need_no_provider(self):
        """Кросс-курс использует старшую дату своей пары и не обновляет свежий кэш."""
        older = self.now - timedelta(seconds=120)
        recent = self.now - timedelta(seconds=20)
        self.store_quotes([("EUR_USD", 2.0, older), ("BTC_USD", 100.0, recent)])
        path = self.storage.data_dir / "rates.json"
        before = path.read_bytes()
        self.assertEqual(self.rates.get_rate("EUR", "USD")["rate"], 2)
        self.assertEqual(self.rates.get_rate("USD", "EUR")["rate"], 0.5)
        cross = self.rates.get_rate("EUR", "BTC")
        self.assertAlmostEqual(cross["rate"], 0.02)
        self.assertEqual((cross["from_code"], cross["to_code"]), ("EUR", "BTC"))
        self.assertEqual(datetime.fromisoformat(cross["updated_at"]), older)
        self.assertEqual(cross["source"], "Fixture")
        self.assertEqual(self.rates.get_rate("USD", "USD")["rate"], 1)
        self.provider.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_stale_leg_refreshes_despite_recent_global_last_refresh(self):
        """TTL проверяется у каждой ноги кросс-курса, а не у корня JSON."""
        self.store_quotes(
            [
                ("EUR_USD", 2.0, self.now - timedelta(seconds=400)),
                ("BTC_USD", 100.0, self.now),
            ]
        )
        self.provider.return_value = {"EUR_USD": 3.0, "BTC_USD": 100.0}
        refreshed = self.rates.get_rate("EUR", "BTC")
        self.assertAlmostEqual(refreshed["rate"], 0.03)
        self.assertEqual(datetime.fromisoformat(refreshed["updated_at"]), self.now)
        self.assertAlmostEqual(self.rates.get_rate("USD", "EUR")["rate"], 1 / 3)
        self.provider.assert_called_once()
        stored = self.storage.load_rates()
        self.assertEqual(stored["pairs"]["EUR_USD"]["rate"], 3)
        self.assertEqual(datetime.fromisoformat(stored["last_refresh"]), self.now)

    def test_failed_refresh_never_replaces_cached_quotes(self):
        """Сбой источника и неправильные котировки не уничтожают прежний кэш."""
        self.store_quotes([("EUR_USD", 2.0, self.now - timedelta(minutes=10))])
        path = self.storage.data_dir / "rates.json"
        before = path.read_bytes()
        self.provider.side_effect = RuntimeError("source unavailable")
        with self.assertRaises(ApiRequestError):
            self.rates.get_rate("EUR", "USD")
        self.assertEqual(path.read_bytes(), before)
        self.provider.side_effect = None
        for quotes in ({"EUR_USD": 0}, {"EUR_USD": True}, {}):
            with self.subTest(quotes=quotes):
                self.provider.return_value = quotes
                with self.assertRaises(ApiRequestError):
                    self.rates.get_rate("EUR", "USD")
                self.assertEqual(path.read_bytes(), before)

    def test_empty_and_zero_balance_portfolios_need_no_quotes_but_validate_base(self):
        """Нулевой портфель оценивается без API, неизвестная база отклоняется."""
        self.login_alice()
        empty = self.trading.show_portfolio("EUR")
        self.assertEqual((empty["base"], empty["rows"], empty["total"]), ("EUR", [], 0))
        portfolio = self.storage.load_portfolios()[0]
        portfolio.add_currency("BTC")
        self.storage.save_portfolios([portfolio])
        zero = self.trading.show_portfolio()
        self.assertEqual(zero["total"], 0)
        self.assertEqual(zero["rows"][0]["value"], 0)
        with self.assertRaises(CurrencyNotFoundError):
            self.trading.show_portfolio("ZZZ")
        self.provider.assert_not_called()

    def test_refresher_updates_cache_and_fresh_quotes_skip_it(self):
        """Parser обновляет тот же кеш, а свежая пара не вызывает новый запрос."""
        self.store_quotes([("EUR_USD", 2, self.now - timedelta(seconds=400))])

        def refresh():
            self.store_quotes([("EUR_USD", 3, self.now)])
            return {"updated_count": 1, "errors": []}

        refresher = Mock(side_effect=refresh)
        rates = RateService(self.storage, clock=lambda: self.now, refresher=refresher)
        self.assertEqual(rates.get_rate("EUR", "USD")["rate"], 3)
        before = (self.storage.data_dir / "rates.json").read_bytes()
        self.assertAlmostEqual(rates.get_rate("USD", "EUR")["rate"], 1 / 3)
        self.assertEqual(rates.get_rate("USD", "USD")["rate"], 1)
        refresher.assert_called_once()
        self.assertEqual((self.storage.data_dir / "rates.json").read_bytes(), before)

    def test_partial_refresh_cannot_make_an_unmodified_stale_leg_fresh(self):
        """Обновление другой пары и корневого времени не отменяет TTL старой ноги."""
        old = self.now - timedelta(seconds=400)
        self.store_quotes([("EUR_USD", 2, old), ("BTC_USD", 100, old)])

        def refresh_other_pair():
            self.store_quotes([("EUR_USD", 2, old), ("BTC_USD", 110, self.now)])
            return {"updated_count": 1, "errors": ["fiat unavailable"]}

        refresher = Mock(side_effect=refresh_other_pair)
        rates = RateService(self.storage, clock=lambda: self.now, refresher=refresher)
        with self.assertRaisesRegex(ApiRequestError, "update-rates"):
            rates.get_rate("EUR", "BTC")
        refresher.assert_called_once()
        self.assertEqual(self.storage.load_rates()["pairs"]["EUR_USD"]["rate"], 2)

    def test_refresher_failure_never_substitutes_demo_rates(self):
        """При отказе Parser отсутствующий курс не заменяется учебным числом."""
        refresher = Mock(side_effect=ApiRequestError("источник недоступен"))
        rates = RateService(self.storage, clock=lambda: self.now, refresher=refresher)
        path = self.storage.data_dir / "rates.json"
        before = path.read_bytes()
        with self.assertRaisesRegex(ApiRequestError, "update-rates"):
            rates.get_rate("EUR", "USD")
        self.assertEqual(path.read_bytes(), before)
        refresher.assert_called_once()

    def test_lazy_parser_uses_current_storage_for_cache_and_history(self):
        """Parser без явного callback пишет обе структуры рядом с переданным Core."""
        rates = RateService(self.storage)
        with (
            patch(
                "valutatrade_hub.parser_service.api_clients.CoinGeckoClient.fetch_rates",
                return_value={"BTC_USD": 100},
            ),
            patch(
                "valutatrade_hub.parser_service.api_clients."
                "ExchangeRateApiClient.fetch_rates",
                return_value={"EUR_USD": 2},
            ),
        ):
            self.assertEqual(rates.get_rate("EUR", "USD")["rate"], 2)
        history = json.loads(
            (self.storage.data_dir / "exchange_rates.json").read_text("utf-8")
        )
        self.assertEqual(
            {record["from_currency"] for record in history}, {"BTC", "EUR"}
        )
        self.assertEqual(self.storage.load_rates()["pairs"]["BTC_USD"]["rate"], 100)

    def test_explicit_numeric_provider_does_not_call_parser_refresher(self):
        """Явный провайдер остаётся изолированным источником для тестов."""
        refresher = Mock(side_effect=AssertionError("Parser must not be called"))
        rates = RateService(
            self.storage,
            provider=self.provider,
            clock=lambda: self.now,
            source="Fixture",
            refresher=refresher,
        )
        self.assertEqual(rates.get_rate("EUR", "USD")["rate"], 2)
        self.provider.assert_called_once()
        refresher.assert_not_called()

    def test_show_rates_reads_cache_and_marks_stale_quotes_without_refresh(self):
        """Просмотр не пишет кеш и честно отмечает дату каждой старой котировки."""
        old = self.now - timedelta(seconds=400)
        self.store_quotes([("EUR_USD", 2, self.now), ("ETH_USD", 10, old)])
        refresher = Mock(side_effect=AssertionError("No network for show-rates"))
        rates = RateService(self.storage, clock=lambda: self.now, refresher=refresher)
        path = self.storage.data_dir / "rates.json"
        before = path.read_bytes()
        report = rates.show_rates()
        self.assertEqual(
            (report["base"], report["last_refresh"]), ("USD", self.now.isoformat())
        )
        self.assertEqual(
            [row["pair"] for row in report["rows"]], ["ETH_USD", "EUR_USD"]
        )
        rows = {row["pair"]: row for row in report["rows"]}
        self.assertTrue(rows["ETH_USD"]["stale"])
        self.assertFalse(rows["EUR_USD"]["stale"])
        self.assertEqual(datetime.fromisoformat(rows["ETH_USD"]["updated_at"]), old)
        self.assertEqual(path.read_bytes(), before)
        refresher.assert_not_called()

    def test_show_rates_filters_crypto_top_and_converts_from_all_cached_pairs(self):
        """Кросс-курс использует обратную USD-ногу, top исключает дорогой фиат."""
        older = self.now - timedelta(seconds=20)
        self.store_quotes(
            [
                ("USD_EUR", 0.5, older),
                ("BTC_USD", 100, self.now),
                ("ETH_USD", 10, self.now),
                ("SOL_USD", 1, self.now),
                ("GBP_USD", 1000, self.now),
            ]
        )
        report = self.rates.show_rates(top=2, base="eur")
        self.assertEqual(
            [row["pair"] for row in report["rows"]], ["BTC_EUR", "ETH_EUR"]
        )
        self.assertEqual([row["rate"] for row in report["rows"]], [50, 5])
        self.assertTrue(
            all(
                datetime.fromisoformat(row["updated_at"]) == older
                for row in report["rows"]
            )
        )
        filtered = self.rates.show_rates(currency=" sol ", base="EUR")
        self.assertEqual(len(filtered["rows"]), 1)
        self.assertEqual(
            (filtered["rows"][0]["pair"], filtered["rows"][0]["rate"]), ("SOL_EUR", 0.5)
        )
        self.provider.assert_not_called()

    def test_show_rates_checks_empty_cache_filters_and_both_leg_timestamps(self):
        """Пустой кеш и неверные фильтры дают ошибки, будущая нога не свежая."""
        with self.assertRaisesRegex(ValueError, "Локальный кеш курсов пуст"):
            self.rates.show_rates()
        self.store_quotes(
            [
                ("EUR_USD", 2, self.now - timedelta(seconds=10)),
                ("BTC_USD", 100, self.now + timedelta(seconds=1)),
            ]
        )
        for top in (0, -1, True, 1.5):
            with self.subTest(top=top), self.assertRaises(ValueError):
                self.rates.show_rates(top=top)
        with self.assertRaisesRegex(ValueError, "'ZZZ' не найден"):
            self.rates.show_rates(currency="ZZZ")
        with self.assertRaises(CurrencyNotFoundError):
            self.rates.show_rates(base="ZZZ")
        row = self.rates.show_rates(currency="BTC", base="EUR")["rows"][0]
        self.assertEqual(row["rate"], 50)
        self.assertTrue(row["stale"])
        self.provider.assert_not_called()

    def test_show_rates_missing_cache_reports_how_to_load_quotes(self):
        """Отсутствующий файл показывается как пустой кеш без обращения к API."""
        (self.storage.data_dir / "rates.json").unlink()
        with self.assertRaisesRegex(ValueError, "Выполните 'update-rates'"):
            self.rates.show_rates()
        self.provider.assert_not_called()

    def test_cli_lifecycle_recovers_from_trade_and_argument_errors(self):
        """Настоящий CLI выполняет сделки и продолжает ввод после ошибок."""
        password = "cli-trading-password"
        self.now = datetime.now(UTC)
        self.store_quotes([("EUR_USD", 2, self.now)])
        commands = (
            "buy --currency EUR --amount 1\n"
            f"register --username Alice --password {password}\n"
            f"login --username Alice --password {password}\n"
            "show-portfolio\n"
            "buy --currency EUR --amount 3\n"
            "sell --currency EUR --amount 1\n"
            "show-portfolio --base EUR\n"
            "sell --currency EUR --amount 100\n"
            "buy --currency BTC --amount not-a-number\n"
            "get-rate --from ZZZ --to USD\n"
            "get-rate --from USD --to EUR\n"
            "logout\n"
            "exit\n"
        )
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
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertNotIn("Traceback", result.stdout)
        self.assertNotIn(password, result.stdout + result.stderr)
        self.assertIn("Недостаточно средств", result.stdout)
        self.assertIn("USD→EUR", result.stdout)
        portfolios = JsonStorage(self.cwd / "data").load_portfolios()
        self.assertEqual(len(portfolios), 1)
        self.assertEqual(set(portfolios[0].wallets), {"EUR"})
        self.assertEqual(portfolios[0].get_wallet("EUR").balance, 2)


if __name__ == "__main__":
    unittest.main()
