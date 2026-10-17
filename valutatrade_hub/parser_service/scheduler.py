"""Периодическое обновление курсов валют."""

import argparse
import logging
import time

from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    ConfigurationError,
    StorageError,
)
from valutatrade_hub.core.utils import validate_number
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.logging_config import configure_logging
from valutatrade_hub.parser_service.config import ParserConfig
from valutatrade_hub.parser_service.storage import RatesStorage
from valutatrade_hub.parser_service.updater import RatesUpdater


def main() -> int:
    """Повторять обновления с заданным интервалом до Ctrl+C."""
    parser = argparse.ArgumentParser(
        description="Периодическое обновление курсов валют"
    )
    parser.add_argument("--source", choices=("coingecko", "exchangerate"))
    parser.add_argument("--interval", type=float)
    options = parser.parse_args()
    try:
        settings = SettingsLoader()
        configure_logging(settings)
        config = ParserConfig.from_settings(settings)
        interval = validate_number(
            config.UPDATE_INTERVAL if options.interval is None else options.interval,
            "interval",
        )
        storage = RatesStorage(config, DatabaseManager(settings))
        updater = RatesUpdater(config=config, storage=storage)
        logger = logging.getLogger("valutatrade.parser")
        print("Parser Service запущен. Для остановки нажмите Ctrl+C.")
        while True:
            try:
                result = updater.run_update(options.source)
                print(
                    f"Обновлено курсов: {result['updated_count']}. "
                    f"Последнее обновление: {result['last_refresh']}"
                )
                if result["errors"]:
                    print("Обновление завершено с ошибками. Проверьте журнал Parser.")
            except (ApiRequestError, StorageError) as error:
                logger.error("%s", error)
                print(error)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nParser Service остановлен.")
        return 0
    except (ConfigurationError, StorageError, ValueError, OSError) as error:
        print(error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
