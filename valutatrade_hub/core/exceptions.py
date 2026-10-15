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
