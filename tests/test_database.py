"""Проверки JSON-хранилища на настоящих временных файлах."""

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from valutatrade_hub.core.exceptions import StorageError
from valutatrade_hub.core.models import Portfolio, User
from valutatrade_hub.infra.database import JsonStorage

DEFAULT_FILES = {
    "users.json": [],
    "portfolios.json": [],
    "rates.json": {"pairs": {}, "last_refresh": None},
    "exchange_rates.json": [],
}


class JsonStorageTests(unittest.TestCase):
    """Проверить сохранность данных, владельцев и обработку ошибок файлов."""

    def setUp(self):
        """Подготовить отдельное хранилище и двух пользователей."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name) / "nested" / "data"
        self.storage = JsonStorage(self.data_dir)
        self.users_path = self.data_dir / "users.json"
        self.portfolios_path = self.data_dir / "portfolios.json"
        self.password = "  секрет пользователя  "
        self.alice = User.create(
            1,
            "Михаил",
            self.password,
            registration_date=datetime(2026, 10, 15, 12, 30, tzinfo=UTC),
        )
        self.bob = User.create(2, "Анна", "another password")

    def write_json(self, filename, payload):
        """Записать внешний JSON, включая заведомо некорректные записи."""
        path = self.data_dir / filename
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def snapshot(self):
        """Прочитать байты всех постоянных файлов для проверки отсутствия изменений."""
        return {
            filename: (self.data_dir / filename).read_bytes()
            for filename in DEFAULT_FILES
        }

    def assert_path_error(self, path, operation):
        """Проверить предметную ошибку и путь к проблемному файлу."""
        with self.assertRaises(StorageError) as caught:
            operation()
        self.assertEqual(Path(caught.exception.path), path)

    def test_initialize_creates_four_defaults_without_accounts_or_money(self):
        """Первый запуск создаёт ожидаемые пустые структуры JSON."""
        self.assertEqual(self.storage.data_dir, self.data_dir)
        for filename, expected in DEFAULT_FILES.items():
            with self.subTest(filename=filename):
                actual = json.loads((self.data_dir / filename).read_text("utf-8"))
                self.assertEqual(actual, expected)
        self.assertEqual(self.storage.load_users(), [])
        self.assertEqual(self.storage.load_portfolios(), [])

    def test_reinitialize_and_new_instance_preserve_existing_bytes(self):
        """Инициализация не очищает и не переформатирует существующие файлы."""
        self.storage.save_users([self.alice])
        portfolio = Portfolio(self.alice)
        portfolio.add_currency("USD").deposit(75)
        self.storage.save_portfolios([portfolio])
        self.write_json(
            "rates.json",
            {
                "pairs": {"EUR_USD": {"rate": 1.1}},
                "last_refresh": "2026-10-15T12:00:00+00:00",
            },
        )
        self.write_json("exchange_rates.json", [{"source": "учебный источник"}])
        for filename in DEFAULT_FILES:
            path = self.data_dir / filename
            path.write_bytes(b" \n" + path.read_bytes() + b"\n\n")
        before = self.snapshot()

        self.storage.initialize()
        restarted = JsonStorage(str(self.data_dir))

        self.assertEqual(self.snapshot(), before)
        self.assertTrue(restarted.load_users()[0].verify_password(self.password))
        self.assertEqual(restarted.load_portfolios()[0].get_wallet("USD").balance, 75)

    def test_initialize_recreates_only_missing_files(self):
        """Отсутствующий файл создаётся, остальные сохраняют свои байты."""
        self.storage.save_users([self.alice])
        before = self.snapshot()
        missing = self.data_dir / "rates.json"
        missing.unlink()

        self.storage.initialize()

        self.assertEqual(
            json.loads(missing.read_text("utf-8")), DEFAULT_FILES[missing.name]
        )
        for filename, content in before.items():
            if filename != missing.name:
                self.assertEqual((self.data_dir / filename).read_bytes(), content)

    def test_initialize_does_not_repair_existing_corrupt_file(self):
        """Повреждённый существующий JSON не заменяется пустыми данными."""
        self.users_path.write_bytes(b"not valid JSON")
        before = self.snapshot()

        JsonStorage(self.data_dir)

        self.assertEqual(self.snapshot(), before)
        self.assert_path_error(self.users_path, self.storage.load_users)

    def test_data_path_that_is_a_file_raises_storage_error(self):
        """Файл вместо каталога данных даёт управляемую ошибку."""
        path = self.data_dir.parent / "not-a-directory"
        path.write_bytes(b"existing content")
        self.assert_path_error(path, lambda: JsonStorage(path))
        self.assertEqual(path.read_bytes(), b"existing content")

    def test_directory_instead_of_json_file_is_rejected_during_initialize(self):
        """Путь существующего каталога не принимается за готовый JSON-файл."""
        self.users_path.unlink()
        self.users_path.mkdir()

        self.assert_path_error(self.users_path, lambda: JsonStorage(self.data_dir))

        self.assertTrue(self.users_path.is_dir())

    def test_users_roundtrip_preserves_cyrillic_date_and_exact_password(self):
        """Хранилище восстанавливает модели с солью и хешем вместо пароля."""
        original = [self.alice, self.bob]
        before = {user.user_id: user.to_dict() for user in original}
        self.storage.save_users(original)

        restored = JsonStorage(self.data_dir).load_users()
        self.assertEqual({user.user_id: user.to_dict() for user in restored}, before)
        by_id = {user.user_id: user for user in restored}
        self.assertIsNot(by_id[1], self.alice)
        self.assertTrue(by_id[1].verify_password(self.password))
        self.assertFalse(by_id[1].verify_password(self.password.strip()))
        self.assertTrue(by_id[2].verify_password("another password"))
        self.assertEqual(by_id[1].username, "Михаил")
        self.assertEqual(by_id[1].registration_date, self.alice.registration_date)
        raw = self.users_path.read_text("utf-8")
        self.assertNotIn(self.password, raw)
        self.assertNotIn("another password", raw)
        self.assertEqual({user.user_id: user.to_dict() for user in original}, before)

    def test_saved_password_change_is_preserved_after_restart(self):
        """Сохранение изменённой модели заменяет действующие пароль и соль."""
        self.storage.save_users([self.alice])
        loaded = self.storage.load_users()[0]
        old_salt = loaded.salt
        new_password = "  новый секрет  "
        loaded.change_password(new_password)

        self.storage.save_users([loaded])

        restored = JsonStorage(self.data_dir).load_users()[0]
        self.assertNotEqual(restored.salt, old_salt)
        self.assertTrue(restored.verify_password(new_password))
        self.assertFalse(restored.verify_password(self.password))
        self.assertEqual(restored.username, self.alice.username)
        self.assertEqual(restored.registration_date, self.alice.registration_date)
        self.assertNotIn(new_password, self.users_path.read_text("utf-8"))

    def test_loaded_objects_are_not_saved_without_explicit_save(self):
        """Изменение восстановленной модели не меняет файл само по себе."""
        self.storage.save_users([self.alice])
        before = self.users_path.read_bytes()
        loaded = self.storage.load_users()[0]
        loaded.username = "Другое имя"
        loaded.change_password("different password")

        self.assertEqual(self.users_path.read_bytes(), before)
        restored = JsonStorage(self.data_dir).load_users()[0]
        self.assertEqual(restored.username, self.alice.username)
        self.assertTrue(restored.verify_password(self.password))

    def test_two_users_keep_independent_wallets_across_restarts(self):
        """Пустые портфели, изменения кошельков и владельцы переживают перезапуск."""
        self.storage.save_users([self.alice, self.bob])
        self.storage.save_portfolios([Portfolio(self.alice), Portfolio(self.bob)])
        restarted = JsonStorage(self.data_dir)
        users = restarted.load_users()
        portfolios = restarted.load_portfolios(users)
        by_id = {portfolio.user_id: portfolio for portfolio in portfolios}
        users_by_id = {user.user_id: user for user in users}
        for user_id, portfolio in by_id.items():
            self.assertIs(portfolio.user, users_by_id[user_id])
            self.assertEqual(portfolio.wallets, {})
        by_id[1].add_currency("USD").deposit(100)
        by_id[2].add_currency("USD").deposit(40)
        restarted.save_portfolios(portfolios)

        second = JsonStorage(self.data_dir)
        portfolios = second.load_portfolios()
        by_id = {portfolio.user_id: portfolio for portfolio in portfolios}
        other_before = by_id[2].to_dict()
        by_id[1].get_wallet("USD").withdraw(25)
        by_id[1].add_currency("EUR").deposit(2.5)
        second.save_portfolios(portfolios)

        final = {
            item.user_id: item for item in JsonStorage(self.data_dir).load_portfolios()
        }
        self.assertEqual(final[1].get_wallet("USD").balance, 75)
        self.assertEqual(final[1].get_wallet("EUR").balance, 2.5)
        self.assertEqual(final[2].to_dict(), other_before)
        self.assertTrue(final[1].user.verify_password(self.password))

    def test_invalid_user_json_and_schema_are_reported_without_rewriting(self):
        """Повреждённые данные пользователей не просачиваются как ошибки Python."""
        invalid_record = self.alice.to_dict()
        invalid_record["registration_date"] = "not-a-date"
        missing_salt = self.alice.to_dict()
        del missing_salt["salt"]
        invalid_id = self.alice.to_dict()
        invalid_id["user_id"] = True
        cases = [b"{", b"null", b"{}", b"[42]", b"[{}]"]
        cases += [
            json.dumps([record]).encode("utf-8")
            for record in (invalid_record, missing_salt, invalid_id)
        ]
        for content in cases:
            with self.subTest(content=content):
                self.users_path.write_bytes(content)
                self.assert_path_error(self.users_path, self.storage.load_users)
                self.assertEqual(self.users_path.read_bytes(), content)

    def test_duplicate_user_ids_and_normalized_names_are_rejected_on_load(self):
        """Пользователи должны иметь уникальные ID и нормализованные имена."""
        same_id = self.bob.to_dict()
        same_id["user_id"] = self.alice.user_id
        same_name = self.bob.to_dict()
        same_name["username"] = f" {self.alice.username} "
        for second in (same_id, same_name):
            with self.subTest(second=second):
                self.write_json("users.json", [self.alice.to_dict(), second])
                before = self.users_path.read_bytes()
                self.assert_path_error(self.users_path, self.storage.load_users)
                self.assertEqual(self.users_path.read_bytes(), before)

    def test_invalid_users_cannot_replace_saved_users(self):
        """Ошибки входного списка не изменяют ранее сохранённых пользователей."""
        self.storage.save_users([self.alice])
        cases = [
            [self.alice, User.create(1, "Другой", "password")],
            [self.alice, User.create(2, self.alice.username, "password")],
            [object()],
            {},
        ]
        for users in cases:
            with self.subTest(users=users):
                before = self.snapshot()
                with self.assertRaises(StorageError):
                    self.storage.save_users(users)
                self.assertEqual(self.snapshot(), before)

    def test_save_users_cannot_orphan_an_existing_portfolio(self):
        """Удаление владельца отклоняется до изменения пользователей или портфелей."""
        self.storage.save_users([self.alice, self.bob])
        self.storage.save_portfolios([Portfolio(self.alice)])
        before = self.snapshot()
        with self.assertRaises(StorageError):
            self.storage.save_users([self.bob])
        self.assertEqual(self.snapshot(), before)

    def test_save_portfolios_requires_saved_and_unique_owners(self):
        """Неизвестные и повторяющиеся владельцы не попадают в JSON."""
        self.storage.save_users([self.alice])
        valid = Portfolio(self.alice)
        cases = [[Portfolio(self.bob)], [valid, valid], [object()], {}]
        for portfolios in cases:
            with self.subTest(portfolios=portfolios):
                before = self.snapshot()
                with self.assertRaises(StorageError):
                    self.storage.save_portfolios(portfolios)
                self.assertEqual(self.snapshot(), before)

    def test_invalid_portfolio_json_and_schema_are_rejected(self):
        """Ошибки структуры, владельцев и балансов дают StorageError."""
        self.storage.save_users([self.alice])
        valid = Portfolio(self.alice).to_dict()
        cases = [
            b"[",
            b"{}",
            b"null",
            b"[false]",
            b"[{}]",
            json.dumps([valid, valid]).encode("utf-8"),
            b'[{"user_id": 99, "wallets": {}}]',
            b'[{"user_id": 1, "wallets": []}]',
            b'[{"user_id": 1, "wallets": {"USD": "invalid"}}]',
            b'[{"user_id": 1, "wallets": {"USD": {"balance": -1}}}]',
            b'[{"user_id": 1, "wallets": {"USD": {"balance": true}}}]',
            b'[{"user_id": 1, "wallets": {"USD": {"balance": NaN}}}]',
            b'[{"user_id": 1, "wallets": {"USD": {"balance": Infinity}}}]',
        ]
        for content in cases:
            with self.subTest(content=content):
                self.portfolios_path.write_bytes(content)
                self.assert_path_error(
                    self.portfolios_path, self.storage.load_portfolios
                )
                self.assertEqual(self.portfolios_path.read_bytes(), content)

    def test_save_does_not_repair_corrupt_existing_target(self):
        """Даже новый валидный payload не затирает повреждённый исходный файл."""
        self.storage.save_users([self.alice])
        operations = [
            (self.users_path, lambda: self.storage.save_users([self.alice])),
            (
                self.portfolios_path,
                lambda: self.storage.save_portfolios([Portfolio(self.alice)]),
            ),
        ]
        for path, save in operations:
            for content in (b"not JSON", b"{}", b"[{}]"):
                with self.subTest(path=path.name, content=content):
                    previous = path.read_bytes()
                    path.write_bytes(content)
                    before = self.snapshot()
                    with self.assertRaises(StorageError):
                        save()
                    self.assertEqual(self.snapshot(), before)
                    path.write_bytes(previous)

    def test_write_failure_preserves_bytes_and_removes_temporary_files(self):
        """Сбой записи или финальной замены сохраняет исходные файлы и каталог."""
        self.storage.save_users([self.alice, self.bob])
        initial = Portfolio(self.alice)
        initial.add_currency("USD").deposit(100)
        self.storage.save_portfolios([initial])
        updated_user = User.create(1, "Новое имя", "new password")
        updated_portfolio = Portfolio(self.alice)
        updated_portfolio.add_currency("USD").deposit(200)
        operations = [
            (
                self.users_path,
                lambda: self.storage.save_users([updated_user, self.bob]),
            ),
            (
                self.portfolios_path,
                lambda: self.storage.save_portfolios([updated_portfolio]),
            ),
        ]
        for path, save in operations:
            for fault_point in ("fsync", "replace"):
                with self.subTest(path=path.name, fault_point=fault_point):
                    before = self.snapshot()
                    existing = set(self.data_dir.iterdir())
                    with patch(
                        f"valutatrade_hub.infra.database.os.{fault_point}",
                        side_effect=OSError("simulated write failure"),
                    ):
                        self.assert_path_error(path, save)
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(set(self.data_dir.iterdir()), existing)
                    restored = JsonStorage(self.data_dir)
                    self.assertTrue(
                        restored.load_users()[0].verify_password(self.password)
                    )
                    self.assertEqual(
                        restored.load_portfolios()[0].get_wallet("USD").balance, 100
                    )


if __name__ == "__main__":
    unittest.main()
