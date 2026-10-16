"""Консольный интерфейс ValutaTrade Hub."""

import argparse
import shlex
import sys

from prettytable import PrettyTable

from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    CurrencyNotFoundError,
    StorageError,
)
from valutatrade_hub.core.usecases import PortfolioService, RateService, UserService
from valutatrade_hub.core.utils import (
    DEFAULT_BASE_CURRENCY,
    DEMO_SOURCE,
    SUPPORTED_CURRENCIES,
    parse_timestamp,
)
from valutatrade_hub.infra.database import JsonStorage

COMMAND_PROMPT = "Введите команду: "
COMMAND_HELP = {
    "register": ("register --username <имя> --password <пароль> — зарегистрироваться"),
    "login": "login --username <имя> --password <пароль> — войти",
    "show-portfolio": "show-portfolio [--base <код>] — показать портфель",
    "buy": "buy --currency <код> --amount <количество> — купить валюту",
    "sell": "sell --currency <код> --amount <количество> — продать валюту",
    "get-rate": "get-rate --from <код> --to <код> — получить курс",
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
    """Подготовить разбор аргументов пользовательских команд."""
    parsers = {}
    for command in ("register", "login"):
        parser = CommandArgumentParser(prog=command, add_help=False, allow_abbrev=False)
        parser.add_argument("--username", required=True)
        parser.add_argument("--password", required=True)
        parsers[command] = parser
    for command in ("buy", "sell"):
        parser = CommandArgumentParser(prog=command, add_help=False, allow_abbrev=False)
        parser.add_argument("--currency", required=True)
        parser.add_argument("--amount", type=float, required=True)
        parsers[command] = parser
    portfolio_parser = CommandArgumentParser(
        prog="show-portfolio", add_help=False, allow_abbrev=False
    )
    portfolio_parser.add_argument("--base", default=DEFAULT_BASE_CURRENCY)
    parsers["show-portfolio"] = portfolio_parser
    rate_parser = CommandArgumentParser(
        prog="get-rate", add_help=False, allow_abbrev=False
    )
    rate_parser.add_argument("--from", dest="from_code", required=True)
    rate_parser.add_argument("--to", dest="to_code", required=True)
    parsers["get-rate"] = rate_parser
    return parsers


def _show_help(arguments: list[str]) -> None:
    """Показать общую справку либо синтаксис одной команды."""
    if not arguments:
        print("\n".join(COMMAND_HELP.values()))
    elif len(arguments) == 1 and arguments[0] in COMMAND_HELP:
        print(COMMAND_HELP[arguments[0]])
        if arguments[0] in ("buy", "sell"):
            print("--amount задаёт количество самой валюты; стоимость указана в USD.")
        if arguments[0] in ("get-rate", "show-portfolio", "buy", "sell"):
            print("Поддерживаемые валюты: " + ", ".join(sorted(SUPPORTED_CURRENCIES)))
    else:
        raise ValueError("Используйте help или help <известная команда>.")


def _print_source(source: str) -> None:
    """Обозначить учебный источник, не выдавая фиксированные курсы за рыночные."""
    if DEMO_SOURCE in source:
        print("Источник: учебная заглушка DemoStub, фиксированные курсы.")
    elif source and source != "identity":
        print(f"Источник: {source}")


def _print_portfolio(report: dict) -> None:
    """Показать кошельки и итоговую стоимость в выбранной базовой валюте."""
    print(f"Портфель пользователя '{report['username']}' (база: {report['base']}):")
    if not report["rows"]:
        print("Портфель пуст. Купите валюту командой buy.")
        return
    table = PrettyTable(["Валюта", "Баланс", f"Стоимость в {report['base']}"])
    table.align["Валюта"] = "l"
    table.align["Баланс"] = "r"
    table.align[f"Стоимость в {report['base']}"] = "r"
    sources = set()
    for row in report["rows"]:
        table.add_row(
            [row["currency_code"], f"{row['balance']:.8g}", f"{row['value']:,.2f}"]
        )
        if row["source"] and row["source"] != "identity":
            sources.add(row["source"])
    print(table)
    print(f"ИТОГО: {report['total']:,.2f} {report['base']}")
    _print_source(", ".join(sorted(sources)))


def _print_trade(action: str, report: dict) -> None:
    """Показать количество валюты, изменение баланса и оценочную стоимость."""
    label = "Покупка" if action == "buy" else "Продажа"
    code = report["currency_code"]
    print(
        f"{label} выполнена: {report['amount']:.8g} {code} "
        f"по курсу {report['rate']:.8g} {report['base']}/{code}"
    )
    print(f"Баланс {code}: было {report['before']:.8g} → стало {report['after']:.8g}")
    estimate = "Оценочная стоимость" if action == "buy" else "Оценочная выручка"
    print(f"{estimate}: {report['value']:,.2f} {report['base']}")
    _print_source(report["source"])


def _print_rate(quote: dict) -> None:
    """Показать прямой и обратный курс вместе со временем обновления."""
    updated_at = parse_timestamp(quote["updated_at"]).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(
        f"Курс {quote['from_code']}→{quote['to_code']}: {quote['rate']:.8g} "
        f"(обновлено: {updated_at})"
    )
    print(
        f"Обратный курс {quote['to_code']}→{quote['from_code']}: "
        f"{1.0 / quote['rate']:.8g}"
    )
    _print_source(quote["source"])


def _execute_command(
    tokens: list[str],
    service: UserService,
    portfolios: PortfolioService,
    rates: RateService,
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
    elif command == "login":
        user = service.login(options.username, options.password)
        print(f"Вы вошли как '{user.username}'")
    elif command == "show-portfolio":
        _print_portfolio(portfolios.show_portfolio(options.base))
    elif command in ("buy", "sell"):
        operation = portfolios.buy if command == "buy" else portfolios.sell
        _print_trade(command, operation(options.currency, options.amount))
    elif command == "get-rate":
        _print_rate(rates.get_rate(options.from_code, options.to_code))
    return True


def main() -> int:
    """Подготовить хранилище и читать команды до exit, EOF или прерывания."""
    try:
        storage = JsonStorage()
        service = UserService(storage)
        rates = RateService(storage)
        portfolios = PortfolioService(storage, service, rates)
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
                if not _execute_command(tokens, service, portfolios, rates, parsers):
                    break
            except StorageError as error:
                print(error)
            except CurrencyNotFoundError as error:
                print(f"Ошибка: {error}. Введите help get-rate.")
            except ApiRequestError as error:
                print(f"{error}. Повторите попытку позже.")
            except (ValueError, PermissionError) as error:
                print(f"Ошибка: {error}")
    except (EOFError, KeyboardInterrupt):
        print()
    return 0
