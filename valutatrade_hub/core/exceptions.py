"""Исключения предметной области ValutaTrade Hub."""

from pathlib import Path


class InsufficientFundsError(ValueError):
    """Недостаточный остаток кошелька для запрошенного списания."""

    def __init__(self, available: float, required: float, code: str) -> None:
        """Сохранить остаток, необходимую сумму и код валюты."""
        self.available = available
        self.required = required
        self.code = code
        super().__init__(
            f"Недостаточно средств: доступно {available:g} {code}, "
            f"требуется {required:g} {code}"
        )


class StorageError(RuntimeError):
    """Ошибка доступа к JSON-файлу либо нарушения его структуры."""

    def __init__(self, path: str | Path, reason: str) -> None:
        """Сохранить путь и безопасное описание причины без содержимого файла."""
        self.path = Path(path)
        super().__init__(f"Ошибка хранилища '{self.path}': {reason}")


class ConfigurationError(ValueError):
    """Не удалось загрузить или проверить конфигурацию приложения."""

    def __init__(self, path: str | Path, reason: str) -> None:
        """Сохранить путь и причину, не включая содержимое настроек."""
        self.path = Path(path)
        super().__init__(f"Ошибка конфигурации '{self.path}': {reason}")


class CurrencyNotFoundError(ValueError):
    """Неизвестная валюта среди поддерживаемых приложением."""

    def __init__(self, code: str) -> None:
        """Сохранить код валюты и подготовить сообщение пользователю."""
        self.code = code
        super().__init__(f"Неизвестная валюта '{code}'")


class ApiRequestError(RuntimeError):
    """Не удалось получить актуальный курс из источника котировок."""

    def __init__(self, reason: str) -> None:
        """Сохранить причину ошибки обращения к источнику курсов."""
        self.reason = reason
        super().__init__(f"Ошибка при обращении к внешнему API: {reason}")
