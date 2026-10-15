"""Исключения предметной области ValutaTrade Hub."""


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
