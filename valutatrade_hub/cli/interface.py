"""Консольный интерфейс ValutaTrade Hub."""

import sys

from valutatrade_hub.core.exceptions import StorageError
from valutatrade_hub.infra.database import JsonStorage


def main() -> int:
    """Подготовить хранилище и запустить консольное приложение."""
    try:
        JsonStorage()
    except StorageError as error:
        print(error, file=sys.stderr)
        return 1
    print("ValutaTrade Hub")
    return 0
