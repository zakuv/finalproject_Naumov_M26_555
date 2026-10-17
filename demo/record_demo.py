"""Записать настоящую сессию CLI в asciinema v2 без сетевых запросов."""

import io
import json
import logging
import os
import time
from contextlib import chdir, contextmanager, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

from valutatrade_hub.cli.interface import main
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.logging_config import ACTION_LOGGER_NAME, PARSER_LOGGER_NAME

CAST_PATH = Path(__file__).resolve().with_name("valutatrade.cast")
COMMAND_DELAY_SECONDS = 1.2
COMMANDS = (
    "register --username student --password Demo1234",
    "login --username student --password Demo1234",
    "buy --currency BTC --amount 0.05",
    "sell --currency BTC --amount 0.01",
    "show-portfolio",
    "get-rate --from BTC --to EUR",
    "update-rates",
    "show-rates --top 2",
    "show-rates --currency RUB",
    "show-rates --base EUR",
    "sell --currency BTC --amount 1",
    "buy --currency ABC --amount 1",
    "exit",
)


def fixture_response(url: str, **kwargs: object) -> requests.Response:
    """Подставить исходные JSON-ответы API из задания вместо HTTP."""
    host = urlsplit(url).hostname
    if host == "api.coingecko.com":
        payload = {
            "bitcoin": {"usd": 59337.21},
            "ethereum": {"usd": 3720.0},
            "solana": {"usd": 145.12},
        }
    elif host == "v6.exchangerate-api.com":
        payload = {
            "result": "success",
            "base_code": "USD",
            "conversion_rates": {"USD": 1, "EUR": 0.927, "GBP": 0.809, "RUB": 98.45},
        }
    else:
        raise RuntimeError("В демо разрешены только два учебных ответа API.")
    response = requests.Response()
    response.status_code = 200
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload).encode("utf-8")
    response.encoding = "utf-8"
    return response


class CastOutput(io.StringIO):
    """Сохранить фактический stdout и временные события asciinema v2."""

    def __init__(self) -> None:
        """Начать отсчёт реального времени с начала записи."""
        super().__init__()
        self.timestamp = int(time.time())
        self.started = time.perf_counter()
        self.events: list[list] = []

    def write(self, text: str) -> int:
        """Добавить вывод с монотонным временем от начала сессии."""
        if text:
            self.events.append(
                [
                    round(time.perf_counter() - self.started, 6),
                    "o",
                    text.replace("\n", "\r\n"),
                ]
            )
        return super().write(text)


@contextmanager
def isolated_logging():
    """Временно отделить файловые обработчики демо от журналов приложения."""
    saved = []
    for name in (ACTION_LOGGER_NAME, PARSER_LOGGER_NAME):
        logger = logging.getLogger(name)
        saved.append((logger, logger.handlers[:], logger.level, logger.propagate))
        logger.handlers = []
    try:
        yield
    finally:
        for logger, handlers, level, propagate in saved:
            for handler in logger.handlers:
                handler.close()
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate


def record_demo() -> Path:
    """Выполнить CLI с подставными ответами и записать его реальный вывод."""
    output = CastOutput()
    commands = iter(COMMANDS)

    def demo_input(prompt: str = "") -> str:
        """Повторить обычное приглашение и ввод команды в записи терминала."""
        time.sleep(COMMAND_DELAY_SECONDS)
        command = next(commands)
        print(prompt + command)
        return command

    with (
        TemporaryDirectory(prefix="valutatrade-demo-") as directory,
        chdir(directory),
        patch.object(SettingsLoader, "_instance", None),
        patch.object(DatabaseManager, "_instance", None),
        isolated_logging(),
        patch.dict(os.environ, {"EXCHANGERATE_API_KEY": "demo-fixture-key"}),
        patch("requests.get", side_effect=fixture_response) as request,
        patch("builtins.input", side_effect=demo_input),
        redirect_stdout(output),
    ):
        print("Воспроизводимая учебная демо-запись: API fixture, без живых запросов.")
        print("Ответы CoinGecko и ExchangeRate-API подставлены из примеров задания.")
        print("Команды выполняет настоящий CLI. Данные и логи — во временной папке.")
        print("Пароль Demo1234 и ключ demo-fixture-key используются только в демо.\n")
        status = main()
        if status != 0 or request.call_count < 2:
            raise RuntimeError("Не удалось выполнить демонстрационную сессию CLI.")

    header = {
        "version": 2,
        "width": 110,
        "height": 32,
        "timestamp": output.timestamp,
        "title": "ValutaTrade Hub: учебная демо-запись с API fixture",
        "env": {"TERM": "xterm-256color", "SHELL": "/bin/sh"},
    }
    with CAST_PATH.open("w", encoding="utf-8") as file:
        for item in (header, *output.events):
            file.write(json.dumps(item, ensure_ascii=False) + "\n")
    return CAST_PATH


if __name__ == "__main__":
    print(f"Демо записано: {record_demo()}")
