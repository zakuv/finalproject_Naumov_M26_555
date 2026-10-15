"""Консольный интерфейс ValutaTrade Hub."""

import argparse
import shlex
import sys

from valutatrade_hub.core.exceptions import StorageError
from valutatrade_hub.core.usecases import UserService
from valutatrade_hub.infra.database import JsonStorage

COMMAND_PROMPT = "Введите команду: "
COMMAND_HELP = {
    "register": ("register --username <имя> --password <пароль> — зарегистрироваться"),
    "login": "login --username <имя> --password <пароль> — войти",
    "logout": "logout — завершить текущую сессию",
    "help": "help [команда] — показать справку",
    "exit": "exit — выйти из приложения",
}


class CommandArgumentParser(argparse.ArgumentParser):
    """Разбирать аргументы без завершения приложения при неверном вводе."""

    def error(self, message: str) -> None:
        """Показать путь к справке, не повторяя введённые пароли в ошибке."""
        raise ValueError(
            f"Неверные аргументы команды '{self.prog}'. Введите help {self.prog}."
        )


def _create_parsers() -> dict[str, CommandArgumentParser]:
    """Подготовить разбор обязательных аргументов регистрации и входа."""
    parsers = {}
    for command in ("register", "login"):
        parser = CommandArgumentParser(prog=command, add_help=False, allow_abbrev=False)
        parser.add_argument("--username", required=True)
        parser.add_argument("--password", required=True)
        parsers[command] = parser
    return parsers


def _show_help(arguments: list[str]) -> None:
    """Показать общую справку либо синтаксис одной команды."""
    if not arguments:
        print("\n".join(COMMAND_HELP.values()))
    elif len(arguments) == 1 and arguments[0] in COMMAND_HELP:
        print(COMMAND_HELP[arguments[0]])
    else:
        raise ValueError("Используйте help или help <известная команда>.")


def _execute_command(
    tokens: list[str],
    service: UserService,
    parsers: dict[str, CommandArgumentParser],
) -> bool:
    """Вызвать сценарий и вернуть, нужно ли продолжать чтение команд."""
    command, *arguments = tokens
    if command == "help":
        _show_help(arguments)
        return True
    if command in ("exit", "logout"):
        if arguments:
            raise ValueError(f"Команда '{command}' не принимает аргументы.")
        if command == "exit":
            return False
        service.logout()
        print("Вы вышли из учётной записи.")
        return True
    if command not in parsers:
        raise ValueError("Неизвестная команда. Введите help.")

    options = parsers[command].parse_args(arguments)
    if command == "register":
        user = service.register(options.username, options.password)
        print(
            f"Пользователь '{user.username}' зарегистрирован (id={user.user_id}). "
            "Войдите: login --username <имя> --password ****"
        )
    else:
        user = service.login(options.username, options.password)
        print(f"Вы вошли как '{user.username}'")
    return True


def main() -> int:
    """Подготовить хранилище и читать команды до exit, EOF или прерывания."""
    try:
        service = UserService(JsonStorage())
    except StorageError as error:
        print(error, file=sys.stderr)
        return 1
    print("ValutaTrade Hub")
    print("Введите help для справки, exit для выхода.")
    parsers = _create_parsers()
    try:
        while True:
            line = input(COMMAND_PROMPT)
            try:
                tokens = shlex.split(line)
            except ValueError:
                print("Ошибка: не удалось разобрать команду. Проверьте кавычки.")
                continue
            if not tokens:
                continue
            try:
                if not _execute_command(tokens, service, parsers):
                    break
            except StorageError as error:
                print(error)
            except (ValueError, PermissionError) as error:
                print(f"Ошибка: {error}")
    except (EOFError, KeyboardInterrupt):
        print()
    return 0
