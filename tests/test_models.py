"""Проверки моделей без файлов, сети и внешних библиотек."""

import hashlib
import json
import unittest
from datetime import datetime, timezone

from valutatrade_hub.core.exceptions import InsufficientFundsError
from valutatrade_hub.core.models import Portfolio, User, Wallet


class UserTests(unittest.TestCase):
    """Проверить учётные данные и безопасное представление пользователя."""

    def setUp(self):
        """Создать пользователя с известной датой регистрации."""
        self.password = "correct password"
        self.registered_at = datetime(2026, 10, 15, 12, 30, tzinfo=timezone.utc)
        self.user = User.create(
            1, " Alice ", self.password, registration_date=self.registered_at
        )

    def test_creation_normalizes_username_and_hashes_password_with_salt(self):
        """Пароль хранится как SHA-256 вместе с отдельной солью."""
        self.assertEqual(self.user.user_id, 1)
        self.assertEqual(self.user.username, "Alice")
        self.assertEqual(self.user.registration_date, self.registered_at)
        self.assertTrue(self.user.salt)
        expected = hashlib.sha256(
            (self.password + self.user.salt).encode("utf-8")
        ).hexdigest()
        self.assertEqual(self.user.hashed_password, expected)
        self.assertNotEqual(self.user.hashed_password, self.password)

    def test_identical_passwords_have_independent_salts(self):
        """Одинаковые пароли разных пользователей не дают одинаковые хеши."""
        other = User.create(2, "Bob", self.password)
        self.assertNotEqual(self.user.salt, other.salt)
        self.assertNotEqual(self.user.hashed_password, other.hashed_password)
        self.assertTrue(other.verify_password(self.password))

    def test_verify_password_checks_exact_password(self):
        """Проверка пароля не меняет учётные данные."""
        before = self.user.to_dict()
        self.assertTrue(self.user.verify_password(self.password))
        self.assertFalse(self.user.verify_password("wrong password"))
        self.assertFalse(self.user.verify_password(self.password + " "))
        self.assertEqual(self.user.to_dict(), before)

    def test_username_change_validates_before_mutation(self):
        """Корректное имя меняется, некорректное сохраняет прежнее значение."""
        self.user.username = " Updated Alice "
        self.assertEqual(self.user.username, "Updated Alice")
        for invalid in ("", "   ", None, 123, True):
            with self.subTest(username=invalid):
                with self.assertRaises(ValueError):
                    self.user.username = invalid
                self.assertEqual(self.user.username, "Updated Alice")

    def test_password_change_replaces_credentials_and_invalidates_old_password(self):
        """Новый пароль получает новую соль и заменяет старый пароль."""
        old_salt = self.user.salt
        old_hash = self.user.hashed_password
        self.user.change_password("new password")
        self.assertNotEqual(self.user.salt, old_salt)
        self.assertNotEqual(self.user.hashed_password, old_hash)
        self.assertTrue(self.user.verify_password("new password"))
        self.assertFalse(self.user.verify_password(self.password))

    def test_invalid_password_change_keeps_previous_credentials(self):
        """Отказ при смене пароля не повреждает действующий пароль."""
        before = self.user.to_dict()
        for invalid in ("", "abc", None, 1234, True):
            with self.subTest(password=invalid):
                with self.assertRaises(ValueError):
                    self.user.change_password(invalid)
                self.assertEqual(self.user.to_dict(), before)
                self.assertTrue(self.user.verify_password(self.password))

    def test_public_user_info_excludes_authentication_secrets(self):
        """Публичная информация содержит только ID, имя и дату."""
        public = self.user.get_user_info()
        self.assertEqual(set(public), {"user_id", "username", "registration_date"})
        self.assertEqual(public["user_id"], self.user.user_id)
        self.assertEqual(public["username"], "Alice")
        parsed_date = datetime.fromisoformat(
            public["registration_date"].replace("Z", "+00:00")
        )
        self.assertEqual(parsed_date, self.registered_at)

    def test_json_roundtrip_preserves_login_without_plaintext_password(self):
        """JSON хранит хеш и соль, а восстановленный пользователь может войти."""
        encoded = json.dumps(self.user.to_dict(), ensure_ascii=False)
        self.assertNotIn(self.password, encoded)
        restored = User.from_dict(json.loads(encoded))
        self.assertEqual(restored.to_dict(), self.user.to_dict())
        self.assertTrue(restored.verify_password(self.password))
        self.assertEqual(restored.registration_date, self.registered_at)

    def test_constructor_accepts_persisted_hash_and_salt(self):
        """Конструктор принимает готовые данные, не хешируя пароль повторно."""
        restored = User(
            self.user.user_id,
            self.user.username,
            self.user.hashed_password,
            self.user.salt,
            self.user.registration_date,
        )
        self.assertEqual(restored.to_dict(), self.user.to_dict())
        self.assertTrue(restored.verify_password(self.password))


class WalletTests(unittest.TestCase):
    """Проверить движение денег и отсутствие изменений после отказа."""

    def test_wallet_normalizes_currency_and_starts_with_zero(self):
        """Код валюты нормализуется, начальный баланс по умолчанию равен нулю."""
        wallet = Wallet(" usd ")
        self.assertEqual(wallet.currency_code, "USD")
        self.assertEqual(wallet.balance, 0)

    def test_deposit_withdraw_and_exact_exhaustion(self):
        """Пополнение и снятие меняют баланс, включая снятие всей суммы."""
        wallet = Wallet("EUR", 25)
        wallet.deposit(12.5)
        self.assertAlmostEqual(wallet.balance, 37.5)
        wallet.withdraw(7.5)
        self.assertAlmostEqual(wallet.balance, 30)
        wallet.withdraw(30)
        self.assertEqual(wallet.balance, 0)

    def test_invalid_initial_balances_are_rejected(self):
        """Баланс не бывает отрицательным, бесконечным или нечисловым."""
        for invalid in (-1, float("nan"), float("inf"), -float("inf"), True, "10"):
            with self.subTest(balance=invalid):
                with self.assertRaises(ValueError):
                    Wallet("USD", invalid)

    def test_invalid_movements_leave_balance_unchanged(self):
        """Некорректные суммы не меняют кошелёк ни при одной операции."""
        wallet = Wallet("USD", 100)
        invalid_amounts = (0, -1, float("nan"), float("inf"), True, "10", None)
        for operation in (wallet.deposit, wallet.withdraw):
            for amount in invalid_amounts:
                with self.subTest(operation=operation.__name__, amount=amount):
                    with self.assertRaises(ValueError):
                        operation(amount)
                    self.assertEqual(wallet.balance, 100)

    def test_insufficient_funds_reports_context_and_preserves_balance(self):
        """Недостаток средств даёт предметную ошибку без списания денег."""
        wallet = Wallet("USD", 20)
        with self.assertRaises(InsufficientFundsError) as caught:
            wallet.withdraw(25)
        self.assertIsInstance(caught.exception, ValueError)
        self.assertEqual(caught.exception.available, 20)
        self.assertEqual(caught.exception.required, 25)
        self.assertEqual(caught.exception.code, "USD")
        self.assertEqual(wallet.balance, 20)

    def test_balance_info_and_json_roundtrip_preserve_currency_and_amount(self):
        """Кошелёк восстанавливается после настоящего JSON roundtrip."""
        wallet = Wallet("btc", 0.125)
        expected = {"currency_code": "BTC", "balance": 0.125}
        self.assertEqual(wallet.get_balance_info(), expected)
        self.assertEqual(wallet.to_dict(), expected)
        restored = Wallet.from_dict(json.loads(json.dumps(wallet.to_dict())))
        self.assertEqual(restored.to_dict(), expected)
        restored.deposit(0.125)
        self.assertAlmostEqual(wallet.balance, 0.125)
        self.assertAlmostEqual(restored.balance, 0.25)


class PortfolioTests(unittest.TestCase):
    """Проверить владение кошельками, оценку и сериализацию портфеля."""

    def setUp(self):
        """Создать независимого владельца для каждого теста."""
        self.user = User.create(1, "Alice", "password")

    def test_owner_is_available_and_cannot_be_reassigned(self):
        """Публичные свойства владельца доступны только для чтения."""
        portfolio = Portfolio(self.user)
        self.assertIs(portfolio.user, self.user)
        self.assertEqual(portfolio.user_id, self.user.user_id)
        with self.assertRaises(AttributeError):
            portfolio.user_id = 2
        with self.assertRaises(AttributeError):
            portfolio.user = User.create(2, "Bob", "password")
        self.assertIs(portfolio.user, self.user)

    def test_initial_wallet_mapping_is_copied_but_wallets_are_shared(self):
        """Изменение входного словаря не меняет состав портфеля."""
        wallet = Wallet("USD", 25)
        initial = {"USD": wallet}
        portfolio = Portfolio(self.user, initial)
        initial.clear()
        self.assertIs(portfolio.get_wallet("USD"), wallet)
        exposed = portfolio.wallets
        self.assertIs(exposed["USD"], wallet)
        exposed.clear()
        self.assertEqual(set(portfolio.wallets), {"USD"})
        wallet.deposit(5)
        self.assertEqual(portfolio.get_wallet("USD").balance, 30)

    def test_initial_wallet_key_must_match_wallet_currency(self):
        """Несовпадающие ключ и валюта кошелька не создают портфель."""
        with self.assertRaises(ValueError):
            Portfolio(self.user, {"EUR": Wallet("USD", 5)})

    def test_wallet_currency_cannot_change_after_adding_to_portfolio(self):
        """Запрет смены валюты сохраняет ключ, баланс и оценку портфеля."""
        portfolio = Portfolio(self.user)
        wallet = portfolio.add_currency("USD")
        wallet.deposit(100)
        with self.assertRaises(AttributeError):
            wallet.currency_code = "BTC"
        self.assertEqual(wallet.currency_code, "USD")
        self.assertEqual(wallet.balance, 100)
        self.assertEqual(set(portfolio.wallets), {"USD"})
        self.assertIs(portfolio.get_wallet("USD"), wallet)
        self.assertEqual(wallet.to_dict(), {"currency_code": "USD", "balance": 100})
        self.assertEqual(portfolio.get_total_value("USD", {}), 100)

    def test_add_currency_is_idempotent_and_missing_wallet_raises(self):
        """Повторное добавление валюты сохраняет существующий баланс."""
        portfolio = Portfolio(self.user)
        first = portfolio.add_currency("usd")
        self.assertIsInstance(first, Wallet)
        first.deposit(50)
        again = portfolio.add_currency(" USD ")
        self.assertIs(first, again)
        self.assertIs(portfolio.get_wallet("usd"), first)
        self.assertEqual(again.balance, 50)
        with self.assertRaises(KeyError):
            portfolio.get_wallet("EUR")

    def test_users_have_independent_wallets(self):
        """Кошельки не разделяются между разными пользователями."""
        other_user = User.create(2, "Bob", "password")
        first = Portfolio(self.user)
        second = Portfolio(other_user)
        first.add_currency("USD").deposit(100)
        second.add_currency("USD").deposit(20)
        first.get_wallet("USD").withdraw(30)
        self.assertEqual(first.get_wallet("USD").balance, 70)
        self.assertEqual(second.get_wallet("USD").balance, 20)

    def test_total_value_converts_direct_inverse_and_usd_cross_rates(self):
        """Один портфель оценивается в USD и EUR без изменения балансов."""
        portfolio = Portfolio(self.user)
        portfolio.add_currency("USD").deposit(10)
        portfolio.add_currency("EUR").deposit(20)
        portfolio.add_currency("BTC").deposit(0.001)
        rates = {"EUR_USD": 2.0, "BTC_USD": 50000.0}
        before = portfolio.to_dict()
        self.assertAlmostEqual(portfolio.get_total_value("USD", rates), 100)
        self.assertAlmostEqual(portfolio.get_total_value("EUR", rates), 50)
        self.assertEqual(portfolio.to_dict(), before)

    def test_default_demo_rates_are_used_only_when_not_overridden(self):
        """Фиктивные курсы работают, а явная пустая карта их не подменяет."""
        portfolio = Portfolio(self.user)
        portfolio.add_currency("EUR").deposit(2)
        portfolio.add_currency("BTC").deposit(0.01)
        portfolio.add_currency("RUB").deposit(100)
        portfolio.add_currency("ETH").deposit(0.5)
        expected = 2 * 1.0786 + 0.01 * 59337.21 + 100 * 0.01016 + 0.5 * 3720
        self.assertAlmostEqual(portfolio.get_total_value(), expected)
        with self.assertRaises(ValueError):
            portfolio.get_total_value(exchange_rates={})

    def test_empty_and_zero_balance_portfolios_need_no_external_rates(self):
        """Для нулевых кошельков не требуется отсутствующий курс."""
        portfolio = Portfolio(self.user)
        self.assertEqual(portfolio.get_total_value(exchange_rates={}), 0)
        portfolio.add_currency("SOL")
        portfolio.add_currency("USD").deposit(12)
        self.assertEqual(portfolio.get_total_value(exchange_rates={}), 12)
        portfolio.get_wallet("SOL").deposit(1)
        with self.assertRaises(ValueError):
            portfolio.get_total_value(exchange_rates={})

    def test_json_roundtrip_preserves_owner_and_independent_wallets(self):
        """JSON портфеля не хранит User, а восстановленные кошельки независимы."""
        portfolio = Portfolio(self.user)
        portfolio.add_currency("USD").deposit(100)
        portfolio.add_currency("BTC").deposit(0.025)
        serialized = portfolio.to_dict()
        self.assertEqual(set(serialized), {"user_id", "wallets"})
        self.assertEqual(serialized["user_id"], self.user.user_id)
        payload = json.loads(json.dumps(serialized))
        restored = Portfolio.from_dict(payload, self.user)
        self.assertIs(restored.user, self.user)
        self.assertEqual(restored.to_dict(), portfolio.to_dict())
        restored.get_wallet("USD").deposit(10)
        self.assertEqual(portfolio.get_wallet("USD").balance, 100)
        self.assertEqual(restored.get_wallet("USD").balance, 110)

    def test_restoring_portfolio_for_different_owner_is_rejected(self):
        """Нельзя восстановить чужой портфель под другим пользователем."""
        portfolio = Portfolio(self.user)
        portfolio.add_currency("USD").deposit(100)
        payload = json.loads(json.dumps(portfolio.to_dict()))
        other_user = User.create(2, "Bob", "password")
        with self.assertRaises(ValueError):
            Portfolio.from_dict(payload, other_user)
        self.assertEqual(portfolio.get_wallet("USD").balance, 100)


if __name__ == "__main__":
    unittest.main()
