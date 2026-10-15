"""Проверки регистрации и сессий поверх настоящего JSON-хранилища."""

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from valutatrade_hub.core.exceptions import StorageError
from valutatrade_hub.core.models import Portfolio, User
from valutatrade_hub.core.usecases import UserService
from valutatrade_hub.infra.database import JsonStorage


class UserServiceTests(unittest.TestCase):
    """Проверить регистрацию, независимые сессии и компенсацию ошибки записи."""

    def setUp(self):
        """Создать отдельное хранилище для каждого сценария."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name) / "data"
        self.storage = JsonStorage(self.data_dir)
        self.service = UserService(self.storage)
        self.password = "  личный тестовый пароль  "

    def file_snapshot(self):
        """Сохранить байты рабочих файлов для проверок отказа без записи."""
        return {path.name: path.read_bytes() for path in self.data_dir.glob("*.json")}

    def object_snapshot(self):
        """Прочитать сохранённые данные без привязки к JSON-форматированию."""
        return {
            "users": {
                user.user_id: user.to_dict() for user in self.storage.load_users()
            },
            "portfolios": {
                portfolio.user_id: portfolio.to_dict()
                for portfolio in self.storage.load_portfolios()
            },
        }

    def test_register_creates_users_and_empty_portfolios_without_logging_in(self):
        """Регистрация сохраняет хеши и пустые кошельки с возрастающими ID."""
        first = self.service.register(" Михаил ", self.password)
        second = self.service.register("Анна", "another password")

        self.assertIsInstance(first, User)
        self.assertEqual(first.username, "Михаил")
        self.assertEqual((first.user_id, second.user_id), (1, 2))
        self.assertIsNone(self.service.current_user)
        restored = JsonStorage(self.data_dir)
        users = {user.user_id: user for user in restored.load_users()}
        self.assertTrue(users[1].verify_password(self.password))
        self.assertTrue(users[2].verify_password("another password"))
        self.assertNotEqual(users[1].salt, users[2].salt)
        portfolios = restored.load_portfolios()
        self.assertEqual({item.user_id for item in portfolios}, {1, 2})
        self.assertTrue(all(item.wallets == {} for item in portfolios))
        raw = (self.data_dir / "users.json").read_text("utf-8")
        self.assertNotIn(self.password, raw)
        self.assertNotIn("another password", raw)

    def test_register_uses_max_id_and_preserves_existing_wallet_balances(self):
        """Новый аккаунт не изменяет сохранённые портфели других пользователей."""
        first = User.create(17, "First", "password")
        second = User.create(4, "Second", "password")
        self.storage.save_users([first, second])
        portfolio = Portfolio(first)
        portfolio.add_currency("USD").deposit(123.5)
        portfolio.add_currency("BTC").deposit(0.01)
        self.storage.save_portfolios([portfolio, Portfolio(second)])
        existing = self.object_snapshot()

        created = self.service.register("New", self.password)

        self.assertEqual(created.user_id, 18)
        after = self.object_snapshot()
        for user_id in (17, 4):
            self.assertEqual(after["users"][user_id], existing["users"][user_id])
            self.assertEqual(
                after["portfolios"][user_id], existing["portfolios"][user_id]
            )
        self.assertEqual(after["portfolios"][18]["wallets"], {})

    def test_registration_preserves_an_existing_session(self):
        """Регистрация второго аккаунта не выполняет вход вместо текущего."""
        self.service.register("First", self.password)
        current = self.service.login("First", self.password)

        self.service.register("Second", "another password")

        self.assertIs(self.service.current_user, current)

    def test_invalid_registration_does_not_write_or_change_session(self):
        """Повторы имени и неверные значения отклоняются до записи файлов."""
        self.service.register("Alice", self.password)
        current = self.service.login("Alice", self.password)
        cases = [
            ("Alice", "different password"),
            (" Alice ", "different password"),
            ("", self.password),
            ("   ", self.password),
            (None, self.password),
            ("New", "abc"),
            ("New", None),
        ]
        for username, password in cases:
            with self.subTest(username=username):
                before = self.file_snapshot()
                with self.assertRaises(ValueError):
                    self.service.register(username, password)
                self.assertEqual(self.file_snapshot(), before)
                self.assertIs(self.service.current_user, current)

    def test_username_uniqueness_is_case_sensitive(self):
        """Разный регистр имён допускает разные учётные записи."""
        first = self.service.register("alice", self.password)
        second = self.service.register("Alice", self.password)
        self.assertNotEqual(first.user_id, second.user_id)
        self.assertEqual(
            {user.username for user in self.storage.load_users()}, {"alice", "Alice"}
        )

    def test_restart_requires_login_and_preserves_exact_password(self):
        """Пароль сохраняется, а сессия не переносится в новый сервис."""
        created = self.service.register("Михаил", self.password)
        self.service.login("Михаил", self.password)
        before = self.file_snapshot()
        restarted = UserService(JsonStorage(self.data_dir))
        self.assertIsNone(restarted.current_user)
        with self.assertRaises(PermissionError):
            restarted.require_login()
        with self.assertRaises(ValueError):
            restarted.login("Михаил", self.password.strip())
        self.assertIsNone(restarted.current_user)

        logged_in = restarted.login(" Михаил ", self.password)

        self.assertEqual(logged_in.user_id, created.user_id)
        self.assertIs(restarted.current_user, logged_in)
        self.assertEqual(self.file_snapshot(), before)

    def test_failed_login_preserves_previous_session(self):
        """Неизвестное имя, неверный пароль и ошибка чтения не сбрасывают вход."""
        self.service.register("First", self.password)
        self.service.register("Second", "another password")
        current = self.service.login("First", self.password)
        before = self.file_snapshot()
        for username, password in (
            ("Missing", self.password),
            ("Second", "wrong password"),
            ("First", self.password.strip()),
        ):
            with self.subTest(username=username):
                with self.assertRaises(ValueError):
                    self.service.login(username, password)
                self.assertIs(self.service.current_user, current)
        failure = StorageError(self.data_dir / "users.json", "simulated read failure")
        with patch.object(self.storage, "load_users", side_effect=failure):
            with self.assertRaises(StorageError) as caught:
                self.service.login("Second", "another password")
        self.assertIs(caught.exception, failure)
        self.assertIs(self.service.current_user, current)
        self.assertEqual(self.file_snapshot(), before)

    def test_logout_and_readonly_session_property(self):
        """Только успешный вход устанавливает сессию; logout возвращает запрет."""
        self.assertIsNone(self.service.current_user)
        with self.assertRaises(PermissionError):
            self.service.require_login()
        self.service.register("First", self.password)
        current = self.service.login("First", self.password)
        self.service.require_login()
        with self.assertRaises(AttributeError):
            self.service.current_user = None
        self.assertIs(self.service.current_user, current)
        before = self.file_snapshot()

        self.service.logout()

        self.assertIsNone(self.service.current_user)
        with self.assertRaises(PermissionError):
            self.service.require_login()
        self.service.logout()
        self.assertEqual(self.file_snapshot(), before)

    def test_core_does_not_print_messages(self):
        """Представление сообщений остаётся обязанностью CLI."""
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.service.register("First", self.password)
            self.service.login("First", self.password)
            self.service.logout()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")

    def test_failed_portfolio_save_rolls_back_users_and_reraises_original_error(self):
        """Отказ второй записи восстанавливает прежние объекты и сессию."""
        self.service.register("First", self.password)
        current = self.service.login("First", self.password)
        portfolio = self.storage.load_portfolios()[0]
        portfolio.add_currency("USD").deposit(75)
        self.storage.save_portfolios([portfolio])
        before = self.object_snapshot()
        failure = StorageError(
            self.data_dir / "portfolios.json", "simulated portfolio write failure"
        )

        with patch.object(self.storage, "save_portfolios", side_effect=failure):
            with self.assertRaises(StorageError) as caught:
                self.service.register("Second", "another password")

        self.assertIs(caught.exception, failure)
        self.assertEqual(self.object_snapshot(), before)
        self.assertIs(self.service.current_user, current)

    def test_failed_users_save_leaves_all_files_unchanged(self):
        """Отказ первой записи не создаёт пользователя или портфель."""
        before = self.file_snapshot()
        failure = StorageError(self.data_dir / "users.json", "simulated write failure")
        with patch.object(self.storage, "save_users", side_effect=failure):
            with self.assertRaises(StorageError) as caught:
                self.service.register("First", self.password)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.file_snapshot(), before)
        self.assertIsNone(self.service.current_user)

    def test_failed_rollback_is_reported_as_failure_without_printing_secrets(self):
        """Двойной отказ не возвращает успешную регистрацию и сохраняет сессию."""
        self.service.register("First", self.password)
        current = self.service.login("First", self.password)
        portfolios_before = self.object_snapshot()["portfolios"]
        save_users = self.storage.save_users
        calls = 0
        registration_error = StorageError(
            self.data_dir / "portfolios.json", "simulated portfolio failure"
        )
        rollback_error = StorageError(
            self.data_dir / "users.json", "simulated rollback failure"
        )

        def save_users_then_fail_rollback(users):
            nonlocal calls
            calls += 1
            if calls == 1:
                save_users(users)
            else:
                raise rollback_error

        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(
                self.storage, "save_users", side_effect=save_users_then_fail_rollback
            ),
            patch.object(
                self.storage, "save_portfolios", side_effect=registration_error
            ),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            with self.assertRaises(StorageError) as caught:
                self.service.register("Second", "another password")

        self.assertIsNot(caught.exception, registration_error)
        self.assertTrue(str(caught.exception).strip())
        self.assertNotIn(self.password, str(caught.exception))
        self.assertNotIn("another password", str(caught.exception))
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")
        self.assertIs(self.service.current_user, current)
        self.assertEqual(self.object_snapshot()["portfolios"], portfolios_before)


if __name__ == "__main__":
    unittest.main()
