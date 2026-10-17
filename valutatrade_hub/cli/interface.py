"""Консольный интерфейс ValutaTrade Hub."""

import argparse
import shlex
import sys

from prettytable import PrettyTable

from valutatrade_hub.core.currencies import get_supported_codes
from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    ConfigurationError,
    CurrencyNotFoundError,
    InsufficientFundsError,
    StorageError,
)
from valutatrade_hub.core.usecases import PortfolioService, RateService, UserService
from valutatrade_hub.core.utils import (
    DEMO_SOURCE,
    parse_timestamp,
)
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.logging_config import configure_logging
from valutatrade_hub.parser_service.config import ParserConfig
from valutatrade_hub.parser_service.storage import RatesStorage
from valutatrade_hub.parser_service.updater import RatesUpdater

COMMAND_PROMPT = "Введите команду: "
COMMAND_HELP = {
    "register": ("register --username <имя> --password <пароль> — зарегистрироваться"),
    "login": "login --username <имя> --password <пароль> — войти",
    "show-portfolio": "show-portfolio [--base <код>] — показать портфель",
    "buy": "buy --currency <код> --amount <количество> — купить валюту",
    "sell": "sell --currency <код> --amount <количество> — продать валюту",
    "get-rate": "get-rate --from <код> --to <код> — получить курс",
    "update-rates": (
        "update-rates [--source coingecko|exchangerate] — обновить курсы из API"
    ),
    "show-rates": (
        "show-rates [--currency <код>] [--top <N>] [--base <код>] — курсы из кэша"
    ),
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
    portfolio_parser.add_argument("--base")
    parsers["show-portfolio"] = portfolio_parser
    rate_parser = CommandArgumentParser(
        prog="get-rate", add_help=False, allow_abbrev=False
    )
    rate_parser.add_argument("--from", dest="from_code", required=True)
    rate_parser.add_argument("--to", dest="to_code", required=True)
    parsers["get-rate"] = rate_parser
    update_parser = CommandArgumentParser(
        prog="update-rates", add_help=False, allow_abbrev=False
    )
    update_parser.add_argument("--source", choices=("coingecko", "exchangerate"))
    parsers["update-rates"] = update_parser
    show_parser = CommandArgumentParser(
        prog="show-rates", add_help=False, allow_abbrev=False
    )
    show_parser.add_argument("--currency")
    show_parser.add_argument("--top", type=int)
    show_parser.add_argument("--base")
    parsers["show-rates"] = show_parser
    return parsers


def _show_help(arguments: list[str]) -> None:
    """Показать общую справку либо синтаксис одной команды."""
    if not arguments:
        print("\n".join(COMMAND_HELP.values()))
    elif len(arguments) == 1 and arguments[0] in COMMAND_HELP:
        print(COMMAND_HELP[arguments[0]])
        if arguments[0] in ("buy", "sell"):
            print("--amount задаёт количество самой валюты; стоимость указана в USD.")
        if arguments[0] in ("get-rate", "show-rates", "show-portfolio", "buy", "sell"):
            print("Поддерживаемые валюты: " + ", ".join(get_supported_codes()))
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
    updater: RatesUpdater | None = None,
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
    elif command == "show-rates":
        _print_rates(rates.show_rates(options.currency, options.top, options.base))
    elif command == "update-rates":
        result = (updater if updater is not None else RatesUpdater()).run_update(
            options.source
        )
        print(
            f"Обновлено курсов: {result['updated_count']}. "
            f"Последнее обновление: {result['last_refresh']}"
        )
        if result["errors"]:
            print("Обновление завершено с ошибками. Подробности: logs/parser.log.")
            for error in result["errors"]:
                print(error)
        else:
            print("Обновление выполнено успешно.")
    return True


def _print_rates(report: dict) -> None:
    """Показать курсы из локального кэша и отметить устаревшие строки."""
    print(f"Курсы из кэша (последнее обновление: {report['last_refresh']}):")
    table = PrettyTable(["Пара", "Курс", "Обновлено (UTC)", "Источник", "Статус"])
    table.align["Курс"] = "r"
    for row in report["rows"]:
        table.add_row(
            [
                row["pair"],
                f"{row['rate']:.10g}",
                parse_timestamp(row["updated_at"]).strftime("%Y-%m-%d %H:%M:%S"),
                row["source"],
                "устарел" if row["stale"] else "свежий",
            ]
        )
    print(table)
    if any(row["stale"] for row in report["rows"]):
        print("Курсы устарели. Выполните update-rates.")


def main() -> int:
    """Подготовить хранилище и читать команды до exit, EOF или прерывания."""
    try:
        settings = SettingsLoader()
        configure_logging(settings)
        storage = DatabaseManager(settings)
        service = UserService(storage)
        config = ParserConfig.from_settings(settings)
        updater = RatesUpdater(storage=RatesStorage(config, storage), config=config)
        rates = RateService(
            storage,
            ttl_seconds=settings.get("rates_ttl_seconds"),
            refresher=updater.run_update,
        )
        portfolios = PortfolioService(
            storage, service, rates, default_base=settings.get("default_base_currency")
        )
    except (ConfigurationError, StorageError) as error:
        print(error, file=sys.stderr)
        return 1
    except OSError:
        print(
            "Не удалось открыть журнал операций. Проверьте путь к логам.",
            file=sys.stderr,
        )
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
                if not _execute_command(
                    tokens, service, portfolios, rates, parsers, updater
                ):
                    break
            except StorageError as error:
                print(error)
            except CurrencyNotFoundError as error:
                print(f"Ошибка: {error}. Введите help get-rate.")
            except InsufficientFundsError as error:
                print(error)
            except ApiRequestError as error:
                print(f"{error}. Проверьте сеть и повторите update-rates позже.")
            except (ValueError, PermissionError) as error:
                print(f"Ошибка: {error}")
    except (EOFError, KeyboardInterrupt):
        print()
    return 0
