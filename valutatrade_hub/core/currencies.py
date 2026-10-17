"""Общий интерфейс и фабрика фиатных и криптовалют."""

from abc import ABC, abstractmethod

from valutatrade_hub.core.exceptions import CurrencyNotFoundError
from valutatrade_hub.core.utils import validate_currency_code, validate_number


def _validate_text(value: str, field_name: str) -> str:
    """Проверить непустой текстовый атрибут валюты."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{field_name}' должен быть непустой строкой.")
    return value.strip()


class Currency(ABC):
    """Валюта с проверенными именем и кодом и общим интерфейсом отображения."""

    def __init__(self, name: str, code: str) -> None:
        """Проверить имя и нормализовать код при создании валюты."""
        self._name = _validate_text(name, "name")
        self._code = validate_currency_code(code)

    @property
    def name(self) -> str:
        """Вернуть человекочитаемое имя валюты."""
        return self._name

    @property
    def code(self) -> str:
        """Вернуть нормализованный код, неизменяемый после создания."""
        return self._code

    @abstractmethod
    def get_display_info(self) -> str:
        """Вернуть сведения о валюте для интерфейса или журнала."""


class FiatCurrency(Currency):
    """Фиатная валюта со страной или зоной эмиссии."""

    def __init__(self, name: str, code: str, issuing_country: str) -> None:
        """Дополнить общие сведения названием эмитента."""
        super().__init__(name, code)
        self._issuing_country = _validate_text(issuing_country, "issuing_country")

    @property
    def issuing_country(self) -> str:
        """Вернуть страну или зону эмиссии."""
        return self._issuing_country

    def get_display_info(self) -> str:
        """Показать код, имя и эмитента фиатной валюты."""
        return f"[FIAT] {self.code} — {self.name} (Issuing: {self.issuing_country})"


class CryptoCurrency(Currency):
    """Криптовалюта с названием алгоритма и известной капитализацией."""

    def __init__(self, name: str, code: str, algorithm: str, market_cap: float) -> None:
        """Проверить алгоритм и неотрицательную конечную капитализацию."""
        super().__init__(name, code)
        self._algorithm = _validate_text(algorithm, "algorithm")
        self._market_cap = validate_number(market_cap, "market_cap", allow_zero=True)

    @property
    def algorithm(self) -> str:
        """Вернуть название алгоритма криптовалюты."""
        return self._algorithm

    @property
    def market_cap(self) -> float:
        """Вернуть капитализацию; ноль обозначает отсутствие рыночных данных."""
        return self._market_cap

    def get_display_info(self) -> str:
        """Показать криптовалюту с капитализацией до трёх значащих цифр."""
        market_cap = f"{self.market_cap:.3g}"
        if "e" in market_cap:
            mantissa, exponent = market_cap.split("e")
            market_cap = f"{mantissa}e{int(exponent)}"
        return (
            f"[CRYPTO] {self.code} — {self.name} "
            f"(Algo: {self.algorithm}, MCAP: {market_cap})"
        )


# Учебные метаданные: капитализация не загружается из сети на этом этапе.
_CURRENCY_FACTORIES = {
    "USD": lambda: FiatCurrency("US Dollar", "USD", "United States"),
    "EUR": lambda: FiatCurrency("Euro", "EUR", "Eurozone"),
    "GBP": lambda: FiatCurrency("Pound Sterling", "GBP", "United Kingdom"),
    "RUB": lambda: FiatCurrency("Russian Ruble", "RUB", "Russia"),
    "BTC": lambda: CryptoCurrency("Bitcoin", "BTC", "SHA-256", 0),
    "ETH": lambda: CryptoCurrency("Ethereum", "ETH", "Ethash", 0),
    "SOL": lambda: CryptoCurrency("Solana", "SOL", "Proof of History", 0),
}


def get_currency(code: str) -> Currency:
    """Создать валюту по коду либо сообщить о неизвестном коде."""
    normalized_code = validate_currency_code(code)
    try:
        factory = _CURRENCY_FACTORIES[normalized_code]
    except KeyError:
        raise CurrencyNotFoundError(normalized_code) from None
    return factory()


def get_supported_codes() -> tuple[str, ...]:
    """Вернуть поддерживаемые коды в алфавитном порядке."""
    return tuple(sorted(_CURRENCY_FACTORIES))
