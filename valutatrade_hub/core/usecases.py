"""Сценарии регистрации, входа и операций с валютами."""

from collections.abc import Callable
from datetime import UTC, datetime

from valutatrade_hub.core.currencies import CryptoCurrency, get_currency
from valutatrade_hub.core.exceptions import ApiRequestError, StorageError
from valutatrade_hub.core.models import Portfolio, User
from valutatrade_hub.core.utils import (
    DEFAULT_BASE_CURRENCY,
    DEMO_SOURCE,
    RATES_TTL_SECONDS,
    parse_timestamp,
    validate_currency_code,
    validate_number,
    validate_password,
    validate_supported_currency,
    validate_username,
)
from valutatrade_hub.decorators import log_action
from valutatrade_hub.infra.database import DatabaseManager, JsonStorage
from valutatrade_hub.infra.settings import SettingsLoader


class UserService:
    """Регистрировать пользователей и хранить текущую сессию в памяти."""

    def __init__(self, storage: JsonStorage | None = None) -> None:
        """Создать независимый сервис без активной сессии."""
        self._storage = storage if storage is not None else DatabaseManager()
        self._current_user = None

    @property
    def current_user(self) -> User | None:
        """Вернуть пользователя текущей сессии без возможности её присвоения."""
        return self._current_user

    def register(self, username: str, password: str) -> User:
        """Сохранить нового пользователя и пустой портфель без автоматического входа."""
        username = validate_username(username)
        password = validate_password(password)
        users = self._storage.load_users()
        portfolios = self._storage.load_portfolios(users)
        if any(user.username == username for user in users):
            raise ValueError(f"Имя пользователя '{username}' уже занято")

        next_user_id = max((user.user_id for user in users), default=0) + 1
        user = User.create(next_user_id, username, password)
        new_users = [*users, user]
        new_portfolios = [*portfolios, Portfolio(user)]
        self._storage.save_users(new_users)
        try:
            self._storage.save_portfolios(new_portfolios)
        except StorageError as registration_error:
            try:
                self._storage.save_users(users)
            except StorageError as rollback_error:
                raise StorageError(
                    registration_error.path,
                    "Регистрация не завершена: не удалось сохранить портфель; "
                    f"отмена изменения '{rollback_error.path}' также не удалась. "
                    "Проверьте файлы пользователей и портфелей.",
                ) from None
            raise
        return user

    def login(self, username: str, password: str) -> User:
        """Проверить учётные данные и заменить сессию только после успешного входа."""
        username = validate_username(username)
        users = self._storage.load_users()
        user = next((user for user in users if user.username == username), None)
        if user is None:
            raise ValueError(f"Пользователь '{username}' не найден")
        if not user.verify_password(password):
            raise ValueError("Неверный пароль")
        self._current_user = user
        return user

    def logout(self) -> None:
        """Завершить текущую сессию, не изменяя файлы данных."""
        self._current_user = None

    def require_login(self) -> User:
        """Вернуть вошедшего пользователя или запросить вход через исключение."""
        if self.current_user is None:
            raise PermissionError("Сначала выполните login")
        return self.current_user


class RateService:
    """Получать курсы из JSON-кеша, обновляя устаревшие через Parser Service."""

    def __init__(
        self,
        storage: JsonStorage | None = None,
        ttl_seconds: float | None = None,
        provider: Callable[[], dict[str, float]] | None = None,
        clock: Callable[[], datetime] | None = None,
        source: str = DEMO_SOURCE,
        *,
        refresher: Callable[[], dict] | None = None,
    ) -> None:
        """Настроить кэш и заменяемые часы без сетевых запросов."""
        if storage is None:
            settings = SettingsLoader()
            storage = DatabaseManager(settings)
            if ttl_seconds is None:
                ttl_seconds = settings.get("rates_ttl_seconds")
        self._storage = storage
        if ttl_seconds is None:
            ttl_seconds = RATES_TTL_SECONDS
        self._ttl_seconds = validate_number(ttl_seconds, "TTL")
        if provider is not None and not callable(provider):
            raise ValueError("Провайдер курсов должен быть вызываемой функцией")
        if refresher is not None and not callable(refresher):
            raise ValueError("Обновление курсов должно быть вызываемой функцией")
        if clock is not None and not callable(clock):
            raise ValueError("Часы должны быть вызываемой функцией")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("Источник курсов должен быть непустой строкой")
        self._provider = provider
        self._refresher = refresher
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._source = source.strip()

    def _now(self) -> datetime:
        """Получить UTC-время; наивное время часов считать временем UTC."""
        now = self._clock()
        if not isinstance(now, datetime):
            raise ValueError("Часы должны возвращать datetime")
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return now.astimezone(UTC)

    def _fresh_quote(
        self,
        pairs: dict,
        source: str,
        target: str,
        now: datetime,
        *,
        allow_stale: bool = False,
    ) -> dict | None:
        """Найти свежую прямую либо обратную котировку по времени самой пары."""
        if source == target:
            quote = {"rate": 1.0, "updated_at": now.isoformat(), "source": "identity"}
            return {**quote, "stale": False} if allow_stale else quote
        for pair, inverse in (
            (f"{source}_{target}", False),
            (f"{target}_{source}", True),
        ):
            quote = pairs.get(pair)
            if quote is None:
                continue
            timestamp = parse_timestamp(quote["updated_at"])
            age = (now - timestamp).total_seconds()
            stale = not 0 <= age < self._ttl_seconds
            if allow_stale or not stale:
                rate = quote["rate"]
                return {
                    "rate": 1.0 / rate if inverse else rate,
                    "updated_at": timestamp.isoformat(),
                    "source": quote["source"],
                    **({"stale": stale} if allow_stale else {}),
                }
        return None

    def _find_quote(
        self,
        pairs: dict,
        source: str,
        target: str,
        now: datetime,
        *,
        allow_stale: bool = False,
        quote_cache: dict | None = None,
    ) -> dict | None:
        """Выбрать прямую пару, обратную либо кросс-курс через USD."""
        cached_quotes = {} if quote_cache is None else quote_cache

        def lookup(from_code: str, to_code: str) -> dict | None:
            """Кешировать ноги курса в пределах одного снимка через замыкание."""
            key = (from_code, to_code, allow_stale)
            if key not in cached_quotes:
                cached_quotes[key] = self._fresh_quote(
                    pairs, from_code, to_code, now, allow_stale=allow_stale
                )
            return cached_quotes[key]

        direct = lookup(source, target)
        if direct is not None:
            return direct
        source_leg = lookup(source, DEFAULT_BASE_CURRENCY)
        target_leg = lookup(target, DEFAULT_BASE_CURRENCY)
        if source_leg is None or target_leg is None:
            return None
        oldest = min(
            parse_timestamp(source_leg["updated_at"]),
            parse_timestamp(target_leg["updated_at"]),
        )
        return {
            "rate": source_leg["rate"] / target_leg["rate"],
            "updated_at": oldest.isoformat(),
            "source": "+".join(sorted({source_leg["source"], target_leg["source"]})),
            **(
                {"stale": source_leg["stale"] or target_leg["stale"]}
                if allow_stale
                else {}
            ),
        }

    def _provider_quotes(self, now: datetime) -> dict:
        """Проверить весь ответ провайдера до изменения кэша."""
        try:
            values = self._provider()
            if not isinstance(values, dict):
                raise ValueError("Провайдер должен вернуть словарь курсов")
            quotes = {}
            for pair, value in values.items():
                if not isinstance(pair, str) or len(pair.split("_")) != 2:
                    raise ValueError("Некорректная валютная пара")
                source, target = pair.split("_")
                if (
                    validate_currency_code(source) != source
                    or validate_currency_code(target) != target
                ):
                    raise ValueError("Коды пары должны быть в верхнем регистре")
                quotes[pair] = {
                    "rate": validate_number(value, "Курс"),
                    "updated_at": now.isoformat(),
                    "source": self._source,
                }
            return quotes
        except Exception:
            raise ApiRequestError(
                "Не удалось обновить курсы у провайдера. Выполните 'update-rates'."
            ) from None

    def _refresh_cache(self) -> None:
        """Попросить Parser Service обновить тот же JSON-кеш, не подменяя курсы."""
        try:
            if self._refresher is None:
                from valutatrade_hub.parser_service.storage import RatesStorage
                from valutatrade_hub.parser_service.updater import RatesUpdater

                storage = RatesStorage(storage=self._storage)
                self._refresher = RatesUpdater(storage=storage).run_update
            self._refresher()
        except StorageError:
            raise
        except ApiRequestError as error:
            raise ApiRequestError(
                f"{error.reason}. Выполните 'update-rates' или повторите позже."
            ) from None
        except Exception:
            raise ApiRequestError(
                "Не удалось обновить курсы. Выполните 'update-rates' "
                "или повторите позже."
            ) from None

    @staticmethod
    def _result(source: str, target: str, quote: dict) -> dict:
        """Проверить вычисленный курс и подготовить DTO для интерфейса."""
        try:
            rate = validate_number(quote["rate"], "Курс")
            validate_number(1.0 / rate, "Обратный курс")
        except (ValueError, OverflowError, ZeroDivisionError):
            raise ApiRequestError(
                f"Курс {source}→{target} невозможно безопасно вычислить. "
                "Выполните 'update-rates'."
            ) from None
        return {"from_code": source, "to_code": target, **quote, "rate": rate}

    def get_rate(self, from_code: str, to_code: str) -> dict:
        """Вернуть свежий курс с источником и временем, не используя устаревший."""
        source = validate_supported_currency(from_code)
        target = validate_supported_currency(to_code)
        now = self._now()
        if source == target:
            quote = self._fresh_quote({}, source, target, now)
            return self._result(source, target, quote)

        snapshot = self._storage.load_rates()
        quote = self._find_quote(snapshot["pairs"], source, target, now)
        if quote is not None:
            return self._result(source, target, quote)

        if self._provider is not None:
            pairs = {**snapshot["pairs"], **self._provider_quotes(now)}
            quote = self._find_quote(pairs, source, target, now)
            if quote is not None:
                result = self._result(source, target, quote)
                self._storage.save_rates(
                    {**snapshot, "pairs": pairs, "last_refresh": now.isoformat()}
                )
                return result
        else:
            self._refresh_cache()
            snapshot = self._storage.load_rates()
            quote = self._find_quote(snapshot["pairs"], source, target, self._now())
        if quote is None:
            raise ApiRequestError(
                f"Курс {source}→{target} недоступен или устарел. "
                "Выполните 'update-rates' или повторите позже."
            )
        return self._result(source, target, quote)

    def show_rates(
        self,
        currency: str | None = None,
        top: int | None = None,
        base: str | None = None,
    ) -> dict:
        """Подготовить таблицу локального кеша, сохранив метки устаревших курсов."""
        base = validate_supported_currency(
            DEFAULT_BASE_CURRENCY if base is None else base
        )
        currency = validate_currency_code(currency) if currency is not None else None
        if top is not None and (type(top) is not int or top <= 0):
            raise ValueError("'top' должен быть положительным целым числом.")
        try:
            snapshot = self._storage.load_rates()
        except StorageError as error:
            if error.path.exists():
                raise
            raise ValueError(
                "Локальный кеш курсов пуст. Выполните 'update-rates', "
                "чтобы загрузить данные."
            ) from None
        pairs = snapshot["pairs"]
        if not pairs:
            raise ValueError(
                "Локальный кеш курсов пуст. Выполните 'update-rates', "
                "чтобы загрузить данные."
            )
        codes = {code for pair in pairs for code in pair.split("_")}
        candidates = {currency} if currency in codes else codes - {base}
        if currency is not None and currency not in codes:
            raise ValueError(f"Курс для '{currency}' не найден в кеше.")
        now = self._now()
        rows = []
        quote_cache = {}
        for code in sorted(candidates):
            if top is not None:
                try:
                    is_crypto = isinstance(get_currency(code), CryptoCurrency)
                except ValueError:
                    is_crypto = False
                if not is_crypto:
                    continue
            quote = self._find_quote(pairs, code, base, now, quote_cache=quote_cache)
            if quote is not None:
                quote = {**quote, "stale": False}
            else:
                quote = self._find_quote(
                    pairs, code, base, now, allow_stale=True, quote_cache=quote_cache
                )
            if quote is None:
                continue
            result = self._result(code, base, quote)
            rows.append(
                {
                    "pair": f"{code}_{base}",
                    "rate": result["rate"],
                    "updated_at": result["updated_at"],
                    "source": result["source"],
                    "stale": result["stale"],
                }
            )
        if not rows:
            if currency is not None:
                raise ValueError(f"Курс для '{currency}' не найден в кеше.")
            raise ValueError(f"Курсы относительно '{base}' не найдены в кеше.")
        if top is not None:
            rows = sorted(rows, key=lambda row: (-row["rate"], row["pair"]))[:top]
        return {
            "rows": rows,
            "last_refresh": snapshot["last_refresh"],
            "base": base,
        }


class PortfolioService:
    """Выполнять учебные сделки и оценивать портфель текущего пользователя."""

    def __init__(
        self,
        storage: JsonStorage,
        users: UserService,
        rates: RateService,
        *,
        default_base: str = DEFAULT_BASE_CURRENCY,
    ) -> None:
        """Связать хранение портфелей, авторизацию и получение котировок."""
        self._storage = storage
        self._users = users
        self._rates = rates
        self._default_base = validate_supported_currency(default_base)

    @property
    def current_user(self) -> User | None:
        """Предоставить журналу операций пользователя текущей сессии."""
        return self._users.current_user

    def _load_portfolio(self, user: User) -> tuple[list[Portfolio], Portfolio]:
        """Прочитать все портфели и найти портфель текущего владельца."""
        portfolios = self._storage.load_portfolios()
        portfolio = next(
            (item for item in portfolios if item.user_id == user.user_id), None
        )
        if portfolio is None:
            raise ValueError("Портфель текущего пользователя не найден")
        return portfolios, portfolio

    def show_portfolio(self, base: str | None = None) -> dict:
        """Подготовить кошельки и итоговую стоимость без изменения их балансов."""
        user = self._users.require_login()
        base = validate_supported_currency(self._default_base if base is None else base)
        _, portfolio = self._load_portfolio(user)
        rows = []
        numeric_rates = {}
        for code, wallet in portfolio.wallets.items():
            value, source = 0.0, "identity"
            if wallet.balance:
                quote = self._rates.get_rate(code, base)
                numeric_rates[f"{code}_{base}"] = quote["rate"]
                value = validate_number(
                    wallet.balance * quote["rate"],
                    "Стоимость кошелька",
                    allow_zero=True,
                )
                source = quote["source"]
            rows.append(
                {
                    "currency_code": code,
                    "balance": wallet.balance,
                    "value": value,
                    "source": source,
                }
            )
        return {
            "username": user.username,
            "base": base,
            "rows": rows,
            "total": portfolio.get_total_value(base, numeric_rates),
        }

    def _trade(self, currency_code: str, amount: float, *, selling: bool) -> dict:
        """Изменить только целевой кошелёк и сохранить все портфели после проверок."""
        user = self._users.require_login()
        code = validate_supported_currency(currency_code)
        amount = validate_number(amount)
        portfolios, portfolio = self._load_portfolio(user)
        wallet = portfolio.wallets.get(code)
        before = wallet.balance if wallet is not None else 0.0
        if selling:
            if wallet is None:
                raise ValueError(f"У вас нет кошелька '{code}'")
            wallet.withdraw(amount)

        quote = self._rates.get_rate(code, DEFAULT_BASE_CURRENCY)
        value = validate_number(amount * quote["rate"], "Оценочная стоимость")
        if not selling:
            wallet = portfolio.add_currency(code)
            wallet.deposit(amount)
        self._storage.save_portfolios(portfolios)
        return {
            "username": user.username,
            "currency_code": code,
            "amount": amount,
            "base": DEFAULT_BASE_CURRENCY,
            "rate": quote["rate"],
            "updated_at": quote["updated_at"],
            "source": quote["source"],
            "before": before,
            "after": wallet.balance,
            "value": value,
        }

    @log_action("BUY", verbose=True)
    def buy(self, currency_code: str, amount: float) -> dict:
        """Пополнить целевую валюту без списания USD; стоимость дать как оценку."""
        return self._trade(currency_code, amount, selling=False)

    @log_action("SELL", verbose=True)
    def sell(self, currency_code: str, amount: float) -> dict:
        """Снять целевую валюту без зачисления USD; стоимость дать как оценку."""
        return self._trade(currency_code, amount, selling=True)
