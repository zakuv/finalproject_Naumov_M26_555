"""Проверки запуска CLI из произвольного рабочего каталога."""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

MAIN_SCRIPT = Path(__file__).resolve().parents[1] / "main.py"
DEFAULT_FILES = {
    "users.json": [],
    "portfolios.json": [],
    "rates.json": {"pairs": {}, "last_refresh": None},
    "exchange_rates.json": [],
}


class CliStartupTests(unittest.TestCase):
    """Проверить первый запуск, повторный запуск и управляемую ошибку IO."""

    def setUp(self):
        """Выделить рабочий каталог, не совпадающий с каталогом проекта."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cwd = Path(temporary.name)

    def run_cli(self):
        """Запустить настоящий main.py отдельным процессом без UV и сети."""
        return subprocess.run(
            [sys.executable, "-B", str(MAIN_SCRIPT)],
            cwd=self.cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )

    def test_first_launch_creates_defaults_and_exits_successfully(self):
        """Команда создаёт хранилище относительно cwd и показывает приветствие."""
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ValutaTrade Hub", result.stdout)
        self.assertEqual(result.stderr, "")
        for filename, expected in DEFAULT_FILES.items():
            with self.subTest(filename=filename):
                path = self.cwd / "data" / filename
                self.assertEqual(json.loads(path.read_text("utf-8")), expected)

    def test_second_launch_preserves_existing_file_contents(self):
        """Повторный запуск не заменяет пользовательские данные стартовыми."""
        first = self.run_cli()
        self.assertEqual(first.returncode, 0, first.stderr)
        data_dir = self.cwd / "data"
        path = data_dir / "users.json"
        path.write_bytes(b" \n[]\n\n")
        history = data_dir / "exchange_rates.json"
        history.write_text('[{"source": "история"}]\n', encoding="utf-8")
        before = {
            filename: (data_dir / filename).read_bytes() for filename in DEFAULT_FILES
        }

        result = self.run_cli()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ValutaTrade Hub", result.stdout)
        self.assertEqual(
            {
                filename: (data_dir / filename).read_bytes()
                for filename in DEFAULT_FILES
            },
            before,
        )

    def test_file_instead_of_data_directory_returns_error_without_traceback(self):
        """Ошибки хранилища дают exit 1 и не раскрывают содержимое файла."""
        path = self.cwd / "data"
        content = "not-a-real-password: приватное содержимое"
        path.write_text(content, encoding="utf-8")

        result = self.run_cli()

        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr.strip())
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn(content, result.stderr)
        self.assertNotIn("ValutaTrade Hub", result.stdout)
        self.assertEqual(path.read_text("utf-8"), content)

    def test_directory_instead_of_users_json_returns_error_without_traceback(self):
        """Неправильный тип пути JSON-файла не выглядит успешным запуском."""
        path = self.cwd / "data" / "users.json"
        path.mkdir(parents=True)

        result = self.run_cli()

        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr.strip())
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("ValutaTrade Hub", result.stdout)
        self.assertTrue(path.is_dir())


if __name__ == "__main__":
    unittest.main()
