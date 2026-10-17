"""Хранение текущих курсов и истории измерений."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from valutatrade_hub.core.exceptions import StorageError
from valutatrade_hub.core.utils import (
    parse_timestamp,
    validate_currency_code,
    validate_number,
)
from valutatrade_hub.infra.database import (
    HISTORY_FILE,
    RATES_FILE,
    DatabaseManager,
    JsonStorage,
)
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.parser_service.config import ParserConfig


def _utc_timestamp(value: str) -> str:
    """Нормализовать время записи в ISO UTC с суффиксом Z."""
    return parse_timestamp(value).isoformat().replace("+00:00", "Z")


def _pair_codes(pair: str) -> tuple[str, str]:
    """Проверить формат валютной пары, не меняя её регистр."""
    if not isinstance(pair, str):
        raise ValueError("Пара должна быть строкой")
    codes = pair.split("_")
    if len(codes) != 2 or any(validate_currency_code(code) != code for code in codes):
        raise ValueError("Некорректная валютная пара")
    return codes[0], codes[1]


class RatesStorage:
    """Обновлять только свежие пары и сохранять уникальные записи истории."""

    def __init__(
        self,
        config: ParserConfig | None = None,
        storage: JsonStorage | None = None,
    ) -> None:
        """Использовать JSON-хранилище Core и настроенный путь истории Parser."""
        if config is None and storage is None:
            settings = SettingsLoader()
            config = ParserConfig.from_settings(settings)
            storage = DatabaseManager(settings)
        elif config is None:
            config = ParserConfig(
                RATES_FILE_PATH=storage._path(RATES_FILE),
                HISTORY_FILE_PATH=storage._path(HISTORY_FILE),
            )
        self.config = config
        if storage is None:
            rates_path = Path(config.RATES_FILE_PATH)
            history_path = Path(config.HISTORY_FILE_PATH)
            storage = JsonStorage(
                rates_path.parent,
                rates_file=rates_path.name,
                history_file=history_path.name,
            )
        self._storage = storage
        self.history_path = Path(config.HISTORY_FILE_PATH)

    def load_rates(self) -> dict:
        """Вернуть проверенный снимок курсов из хранилища основного сервиса."""
        return self._storage.load_rates()

    def load_history(self) -> list[dict]:
        """Прочитать историю, не заменяя повреждённые данные пустым списком."""
        try:
            with self.history_path.open("r", encoding="utf-8") as file:
                history = json.load(file)
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError, ValueError, RecursionError) as error:
            raise StorageError(
                self.history_path,
                f"не удалось прочитать историю ({type(error).__name__})",
            ) from None
        try:
            if not isinstance(history, list):
                raise ValueError("История должна быть списком")
            ids = set()
            for record in history:
                if not isinstance(record, dict):
                    raise ValueError("Некорректная запись истории")
                pair = f"{record['from_currency']}_{record['to_currency']}"
                _pair_codes(pair)
                timestamp = _utc_timestamp(record["timestamp"])
                if record["id"] != f"{pair}_{timestamp}" or record["id"] in ids:
                    raise ValueError("Некорректный или повторяющийся ID")
                ids.add(record["id"])
                validate_number(record["rate"], "Курс")
                if (
                    not isinstance(record["source"], str)
                    or not record["source"].strip()
                ):
                    raise ValueError("Не указан источник")
                if not isinstance(record.get("meta", {}), dict):
                    raise ValueError("Некорректные метаданные")
            json.dumps(history, allow_nan=False)
        except (KeyError, TypeError, ValueError, RecursionError):
            raise StorageError(
                self.history_path, "некорректная структура истории курсов"
            ) from None
        return history

    def _save_history(self, history: list[dict]) -> None:
        """Записать историю через временный файл и атомарный os.replace."""
        temporary_path = None
        try:
            text = json.dumps(history, ensure_ascii=False, indent=4, allow_nan=False)
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.history_path.parent,
                prefix=f".{self.history_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary_path = Path(file.name)
                file.write(text + "\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.history_path)
            temporary_path = None
        except (OSError, UnicodeError, TypeError, ValueError, RecursionError) as error:
            raise StorageError(
                self.history_path,
                f"не удалось сохранить историю ({type(error).__name__})",
            ) from None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def save_rates(self, quotes: dict[str, dict], last_refresh: str) -> dict:
        """Сохранить более новые срезы и историю без дублирования измерений."""
        snapshot = self.load_rates()
        history = self.load_history()
        ids = {record["id"] for record in history}
        pairs = dict(snapshot["pairs"])
        additions = []
        changed = 0
        try:
            refresh = _utc_timestamp(last_refresh)
            if not isinstance(quotes, dict):
                raise ValueError("Ожидается словарь котировок")
            for pair, quote in quotes.items():
                source, target = _pair_codes(pair)
                rate = validate_number(quote["rate"], "Курс")
                timestamp = _utc_timestamp(quote["updated_at"])
                origin = quote["source"]
                if not isinstance(origin, str) or not origin.strip():
                    raise ValueError("Не указан источник")
                meta = quote.get("meta", {})
                if not isinstance(meta, dict):
                    raise ValueError("Некорректные метаданные")
                json.dumps(meta, allow_nan=False)
                identifier = f"{pair}_{timestamp}"
                if identifier not in ids:
                    additions.append(
                        {
                            "id": identifier,
                            "from_currency": source,
                            "to_currency": target,
                            "rate": rate,
                            "timestamp": timestamp,
                            "source": origin,
                            "meta": meta,
                        }
                    )
                    ids.add(identifier)
                previous = pairs.get(pair)
                if previous is not None and parse_timestamp(timestamp) <= (
                    parse_timestamp(previous["updated_at"])
                ):
                    continue
                pairs[pair] = {
                    "rate": rate,
                    "updated_at": timestamp,
                    "source": origin,
                }
                changed += 1
        except (KeyError, TypeError, ValueError, RecursionError):
            raise StorageError(
                self.history_path, "некорректные данные полученных котировок"
            ) from None
        if additions:
            self._save_history([*history, *additions])
        if not changed:
            return {"updated_count": 0, "last_refresh": snapshot["last_refresh"]}
        updated = {
            **snapshot,
            "pairs": pairs,
            "source": "ParserService",
            "last_refresh": refresh,
        }
        self._storage.save_rates(updated)
        return {"updated_count": changed, "last_refresh": refresh}
