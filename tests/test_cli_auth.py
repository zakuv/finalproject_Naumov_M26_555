"""Проверки команд авторизации через настоящий процесс CLI."""

import io
import json
import subprocess
import sys
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from valutatrade_hub.cli.interface import main
from valutatrade_hub.core.models import User

MAIN_SCRIPT = Path(__file__).resolve().parents[1] / "main.py"


class CliAuthTests(unittest.TestCase):
    """Проверить успешные команды, ошибки ввода и завершение REPL."""

    def setUp(self):
        """Выделить независимые JSON-файлы и рабочий каталог."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cwd = Path(temporary.name)

    def run_cli(self, commands):
        """Передать команды в stdin, исключив ожидание терминального ввода."""
        return subprocess.run(
            [sys.executable, "-B", str(MAIN_SCRIPT)],
            cwd=self.cwd,
            input=commands,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )

    def assert_clean_exit(self, result):
        """Проверить штатное завершение без traceback и сообщений в stderr."""
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertNotIn("Traceback", result.stdout)
        self.assertIn("ValutaTrade Hub", result.stdout)

    def test_register_login_logout_and_help(self):
        """Основные команды выполняются последовательно в одном процессе."""
        password = "unique-test-password"
        result = self.run_cli(
            "help\n"
            "help login\n"
            f"register --username Alice --password {password}\n"
            f"login --username Alice --password {password}\n"
            "logout\n"
            "exit\n"
        )
        self.assert_clean_exit(result)
        self.assertIn("зарегистрирован", result.stdout)
        self.assertIn("Вы вошли как 'Alice'", result.stdout)
        self.assertIn("Вы вышли", result.stdout)
        for command in ("register", "login", "logout", "help", "exit"):
            self.assertIn(command, result.stdout)
        self.assertNotIn(password, result.stdout + result.stderr)
        users = json.loads((self.cwd / "data/users.json").read_text("utf-8"))
        portfolios = json.loads((self.cwd / "data/portfolios.json").read_text("utf-8"))
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["username"], "Alice")
        self.assertTrue(User.from_dict(users[0]).verify_password(password))
        self.assertEqual(portfolios, [{"user_id": users[0]["user_id"], "wallets": {}}])

    def test_quoted_username_and_password_keep_interior_and_password_spaces(self):
        """Shlex сохраняет имя из двух слов и значимые пробелы пароля."""
        username = "Михаил Наумов"
        password = "  мой секрет  "
        result = self.run_cli(
            f'register --username "{username}" --password "{password}"\n'
            f'login --username "{username}" --password "{password.strip()}"\n'
            f'login --username "{username}" --password "{password}"\n'
            "exit\n"
        )
        self.assert_clean_exit(result)
        self.assertIn(username, result.stdout)
        self.assertIn("Ошибка:", result.stdout)
        self.assertEqual(result.stdout.count("Вы вошли как"), 1)
        self.assertNotIn(password.strip(), result.stdout + result.stderr)
        payload = json.loads((self.cwd / "data/users.json").read_text("utf-8"))
        user = User.from_dict(payload[0])
        self.assertEqual(user.username, username)
        self.assertTrue(user.verify_password(password))
        self.assertFalse(user.verify_password(password.strip()))

    def test_invalid_commands_arguments_and_quotes_recover_without_password_echo(self):
        """Ошибки синтаксиса не завершают REPL и не повторяют введённый пароль."""
        password = "sensitive-test-password"
        result = self.run_cli(
            "\n"
            "missing-command\n"
            "help missing-command\n"
            "login --username Unknown\n"
            f"register --password {password}\n"
            f"login --username Unknown --password {password} {password}\n"
            f'register --username "unterminated --password {password}\n'
            "register --username Short --password abc\n"
            "logout unexpected-argument\n"
            "exit unexpected-argument\n"
            f"register --username Good --password {password}\n"
            f"login --username Good --password {password}\n"
            "exit\n"
        )
        self.assert_clean_exit(result)
        self.assertIn("Ошибка:", result.stdout)
        self.assertIn("Вы вошли как 'Good'", result.stdout)
        self.assertNotIn(password, result.stdout + result.stderr)
        users = json.loads((self.cwd / "data/users.json").read_text("utf-8"))
        self.assertEqual([user["username"] for user in users], ["Good"])

    def test_unknown_user_wrong_password_and_duplicate_name_do_not_stop_loop(self):
        """Предметные ошибки не создают повторный аккаунт и допускают новый ввод."""
        password = "another-sensitive-password"
        result = self.run_cli(
            f"register --username Alice --password {password}\n"
            f"register --username Alice --password {password}\n"
            f"login --username Missing --password {password}\n"
            "login --username Alice --password wrong-password\n"
            f"login --username Alice --password {password}\n"
            "exit\n"
        )
        self.assert_clean_exit(result)
        self.assertIn("Ошибка:", result.stdout)
        self.assertEqual(result.stdout.count("зарегистрирован"), 1)
        self.assertEqual(result.stdout.count("Вы вошли как"), 1)
        self.assertNotIn(password, result.stdout + result.stderr)
        users = json.loads((self.cwd / "data/users.json").read_text("utf-8"))
        self.assertEqual(len(users), 1)

    def test_eof_after_commands_exits_successfully(self):
        """Закрытие stdin завершает REPL и не требует отдельной команды exit."""
        result = self.run_cli("help\n")
        self.assert_clean_exit(result)
        self.assertIn("register --username", result.stdout)

    def test_keyboard_interrupt_exits_successfully(self):
        """Прерывание во время ввода обрабатывается как нормальное завершение."""
        output, errors = io.StringIO(), io.StringIO()
        with (
            chdir(self.cwd),
            patch("builtins.input", side_effect=KeyboardInterrupt),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            status = main()
        self.assertEqual(status, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertIn("ValutaTrade Hub", output.getvalue())


if __name__ == "__main__":
    unittest.main()
