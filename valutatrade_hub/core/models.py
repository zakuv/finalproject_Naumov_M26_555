"""Модели пользователей, кошельков и портфелей."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from valutatrade_hub.core.exceptions import InsufficientFundsError
from valutatrade_hub.core.utils import (
    get_exchange_rate,
    validate_currency_code,
    validate_number,
    validate_password,
    validate_user_id,
    validate_username,
)

SALT_BYTES = 16


class User:
    """Хранить данные пользователя и проверять его пароль."""

    def __init__(
        self,
        user_id: int,
        username: str,
        hashed_password: str,
        salt: str,
        registration_date: datetime,
    ) -> None:
        """Восстановить пользователя с готовым хешем и датой регистрации."""
        self._user_id = validate_user_id(user_id)
        self.username = username
        if not isinstance(hashed_password, str) or not hashed_password:
            raise ValueError("Хеш пароля должен быть непустой строкой")
        if not isinstance(salt, str) or not salt:
            raise ValueError("Соль должна быть непустой строкой")
        if not isinstance(registration_date, datetime):
            raise ValueError("Дата регистрации должна иметь тип datetime")
        self._hashed_password = hashed_password
        self._salt = salt
        self._registration_date = registration_date

    @classmethod
    def create(
        cls,
        user_id: int,
        username: str,
        password: str,
        registration_date: datetime | None = None,
    ) -> User:
        """Создать пользователя с новой солью, не сохраняя исходный пароль."""
        password = validate_password(password)
        salt = secrets.token_hex(SALT_BYTES)
        if registration_date is None:
            registration_date = datetime.now(UTC)
        return cls(
            user_id,
            username,
            cls._hash_password(password, salt),
            salt,
            registration_date,
        )

    @staticmethod
    def _hash_password(password: str, salt: str) -> str:
        """Получить SHA-256 от пароля с пользовательской солью."""
        return hashlib.sha256((password + salt).encode("utf-8")).hexdigest()

    @property
    def user_id(self) -> int:
        """Вернуть идентификатор пользователя."""
        return self._user_id

    @property
    def username(self) -> str:
        """Вернуть имя пользователя."""
        return self._username

    @username.setter
    def username(self, value: str) -> None:
        """Установить непустое имя пользователя."""
        self._username = validate_username(value)

    @property
    def hashed_password(self) -> str:
        """Вернуть сохранённый хеш для записи в хранилище."""
        return self._hashed_password

    @property
    def salt(self) -> str:
        """Вернуть соль для записи в хранилище."""
        return self._salt

    @property
    def registration_date(self) -> datetime:
        """Вернуть дату регистрации пользователя."""
        return self._registration_date

    def get_user_info(self) -> dict:
        """Вернуть данные пользователя без хеша пароля и соли."""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "registration_date": self.registration_date.isoformat(),
        }

    def verify_password(self, password: str) -> bool:
        """Сравнить хеш введённого пароля с сохранённым хешем."""
        if not isinstance(password, str):
            return False
        actual_hash = self._hash_password(password, self.salt)
        return hmac.compare_digest(
            actual_hash.encode("utf-8"), self.hashed_password.encode("utf-8")
        )

    def change_password(self, new_password: str) -> None:
        """Заменить пароль и соль после успешной проверки нового пароля."""
        new_password = validate_password(new_password)
        new_salt = secrets.token_hex(SALT_BYTES)
        new_hash = self._hash_password(new_password, new_salt)
        self._salt = new_salt
        self._hashed_password = new_hash

    def to_dict(self) -> dict:
        """Подготовить пользователя к сохранению в JSON."""
        return {
            **self.get_user_info(),
            "hashed_password": self.hashed_password,
            "salt": self.salt,
        }

    @classmethod
    def from_dict(cls, data: dict) -> User:
        """Восстановить пользователя из JSON-совместимого словаря."""
        return cls(
            data["user_id"],
            data["username"],
            data["hashed_password"],
            data["salt"],
            datetime.fromisoformat(data["registration_date"]),
        )


class Wallet:
    """Хранить баланс пользователя в одной валюте."""

    def __init__(self, currency_code: str, balance: float = 0.0) -> None:
        """Создать кошелёк с неотрицательным балансом."""
        self._currency_code = validate_currency_code(currency_code)
        self.balance = balance

    @property
    def currency_code(self) -> str:
        """Вернуть валюту кошелька, установленную при его создании."""
        return self._currency_code

    @property
    def balance(self) -> float:
        """Вернуть текущий баланс."""
        return self._balance

    @balance.setter
    def balance(self, value: float) -> None:
        """Установить конечный неотрицательный числовой баланс."""
        self._balance = validate_number(value, "Баланс", allow_zero=True)

    def deposit(self, amount: float) -> None:
        """Пополнить кошелёк на положительную сумму."""
        amount = validate_number(amount)
        self.balance += amount

    def withdraw(self, amount: float) -> None:
        """Снять положительную сумму при достаточном балансе."""
        amount = validate_number(amount)
        if amount > self.balance:
            raise InsufficientFundsError(self.balance, amount, self.currency_code)
        self.balance -= amount

    def get_balance_info(self) -> dict:
        """Вернуть код валюты и текущий баланс для вывода в интерфейсе."""
        return {"currency_code": self.currency_code, "balance": self.balance}

    def to_dict(self) -> dict:
        """Подготовить кошелёк к сохранению в JSON."""
        return self.get_balance_info()

    @classmethod
    def from_dict(cls, data: dict) -> Wallet:
        """Восстановить кошелёк из JSON-совместимого словаря."""
        return cls(data["currency_code"], data["balance"])


class Portfolio:
    """Объединять кошельки одного пользователя и оценивать их стоимость."""

    def __init__(self, user: User, wallets: dict[str, Wallet] | None = None) -> None:
        """Создать портфель пользователя с отдельным словарём кошельков."""
        if not isinstance(user, User):
            raise ValueError("Владельцем портфеля должен быть объект User")
        if wallets is not None and not isinstance(wallets, dict):
            raise ValueError("Кошельки должны быть словарём")
        self._user_id = user.user_id
        self._user = user
        self._wallets = {} if wallets is None else wallets.copy()
        for code, wallet in self._wallets.items():
            if (
                validate_currency_code(code) != code
                or not isinstance(wallet, Wallet)
                or wallet.currency_code != code
            ):
                raise ValueError("Ключ кошелька должен совпадать с его кодом валюты")

    @property
    def user_id(self) -> int:
        """Вернуть идентификатор владельца портфеля."""
        return self._user_id

    @property
    def user(self) -> User:
        """Вернуть владельца портфеля без возможности замены."""
        return self._user

    @property
    def wallets(self) -> dict[str, Wallet]:
        """Вернуть копию словаря с действующими объектами кошельков."""
        return self._wallets.copy()

    def add_currency(self, currency_code: str) -> Wallet:
        """Создать новый кошелёк или вернуть уже существующий."""
        currency_code = validate_currency_code(currency_code)
        if currency_code not in self._wallets:
            self._wallets[currency_code] = Wallet(currency_code)
        return self._wallets[currency_code]

    def get_wallet(self, currency_code: str) -> Wallet:
        """Вернуть кошелёк; отсутствие кошелька обозначить через KeyError."""
        return self._wallets[validate_currency_code(currency_code)]

    def get_total_value(
        self,
        base_currency: str = "USD",
        exchange_rates: dict[str, float] | None = None,
    ) -> float:
        """Посчитать стоимость ненулевых балансов по переданным курсам."""
        base_currency = validate_currency_code(base_currency)
        total = 0.0
        for code, wallet in self._wallets.items():
            if wallet.balance == 0:
                continue
            rate = get_exchange_rate(code, base_currency, exchange_rates)
            total += wallet.balance * rate
        return validate_number(total, "Стоимость портфеля", allow_zero=True)

    def to_dict(self) -> dict:
        """Подготовить идентификатор и балансы портфеля для JSON."""
        return {
            "user_id": self.user_id,
            "wallets": {
                code: {"balance": wallet.balance}
                for code, wallet in self._wallets.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict, user: User) -> Portfolio:
        """Восстановить портфель, проверив его принадлежность пользователю."""
        if not isinstance(user, User):
            raise ValueError("Владельцем портфеля должен быть объект User")
        user_id = validate_user_id(data["user_id"])
        if user_id != user.user_id:
            raise ValueError("Портфель принадлежит другому пользователю")
        if not isinstance(data["wallets"], dict):
            raise ValueError("Кошельки должны быть словарём")
        wallets = {}
        for code, details in data["wallets"].items():
            if details.get("currency_code", code) != code:
                raise ValueError("Ключ кошелька должен совпадать с его кодом валюты")
            wallets[code] = Wallet(code, details["balance"])
        return cls(user, wallets)
