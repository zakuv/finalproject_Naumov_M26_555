"""Доступ к JSON-хранилищу пользователей, портфелей и курсов валют."""

import json
import os
import tempfile
from pathlib import Path

from valutatrade_hub.core.exceptions import ConfigurationError, StorageError
from valutatrade_hub.core.models import Portfolio, User
from valutatrade_hub.core.utils import (
    parse_timestamp,
    validate_currency_code,
    validate_number,
)
from valutatrade_hub.infra.settings import (
    DEFAULT_CONFIG_FILE,
    JSON_FILE_KEYS,
    SettingsLoader,
)

DEFAULT_DATA_DIR = "data"
USERS_FILE = "users.json"
PORTFOLIOS_FILE = "portfolios.json"
RATES_FILE = "rates.json"
HISTORY_FILE = "exchange_rates.json"


def _reject_nonfinite_number(value: str) -> None:
    """Запретить не входящие в JSON литералы NaN и Infinity."""
    raise ValueError("Неконечные числа недопустимы в JSON")


class JsonStorage:
    """Хранить пользователей и портфели с атомарной заменой отдельных файлов."""

    def __init__(
        self,
        data_dir: str | Path = DEFAULT_DATA_DIR,
        *,
        users_file: str = USERS_FILE,
        portfolios_file: str = PORTFOLIOS_FILE,
        rates_file: str = RATES_FILE,
        history_file: str = HISTORY_FILE,
    ) -> None:
        """Запомнить каталог и создать только отсутствующие файлы данных."""
        try:
            self.data_dir = Path(data_dir)
        except (TypeError, ValueError):
            raise StorageError(DEFAULT_DATA_DIR, "некорректный путь каталога") from None
        self._filenames = {
            USERS_FILE: users_file,
            PORTFOLIOS_FILE: portfolios_file,
            RATES_FILE: rates_file,
            HISTORY_FILE: history_file,
        }
        filenames = list(self._filenames.values())
        if any(
            not isinstance(filename, str)
            or not filename.strip()
            or "/" in filename
            or "\\" in filename
            or "\x00" in filename
            or not filename.endswith(".json")
            for filename in filenames
        ):
            raise StorageError(self.data_dir, "ожидаются имена JSON-файлов без пути")
        if len({filename.casefold() for filename in filenames}) != len(filenames):
            raise StorageError(self.data_dir, "имена JSON-файлов должны различаться")
        self.initialize()

    def _path(self, filename: str) -> Path:
        """Получить путь к настроенному файлу по его стандартному имени."""
        return self.data_dir / self._filenames[filename]

    def initialize(self) -> None:
        """Идемпотентно создать каталог и начальные файлы, не меняя существующие."""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as error:
            raise StorageError(
                self.data_dir,
                f"не удалось создать каталог ({type(error).__name__})",
            ) from None

        defaults = {
            USERS_FILE: [],
            PORTFOLIOS_FILE: [],
            RATES_FILE: {"pairs": {}, "last_refresh": None},
            HISTORY_FILE: [],
        }
        for filename, initial_data in defaults.items():
            path = self._path(filename)
            text = json.dumps(
                initial_data, ensure_ascii=False, indent=4, allow_nan=False
            )
            try:
                with path.open("x", encoding="utf-8") as file:
                    file.write(text + "\n")
                    file.flush()
                    os.fsync(file.fileno())
            except FileExistsError:
                try:
                    if path.is_file():
                        continue
                except OSError:
                    pass
                raise StorageError(
                    path, "вместо JSON-файла найден каталог или недоступный объект"
                ) from None
            except (OSError, UnicodeError, ValueError) as error:
                raise StorageError(
                    path,
                    f"не удалось создать начальный файл ({type(error).__name__})",
                ) from None

    @staticmethod
    def _validate_records(data: object, path: Path) -> None:
        """Проверить общий формат: список записей в виде словарей."""
        if not isinstance(data, list) or not all(
            isinstance(entry, dict) for entry in data
        ):
            raise StorageError(path, "ожидается список JSON-объектов")

    def _read_json(self, filename: str) -> object:
        """Прочитать JSON без подмены повреждённых данных значениями по умолчанию."""
        path = self._path(filename)
        try:
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file, parse_constant=_reject_nonfinite_number)
        except json.JSONDecodeError as error:
            raise StorageError(
                path,
                f"некорректный JSON, строка {error.lineno}, столбец {error.colno}",
            ) from None
        except (OSError, UnicodeError, ValueError, RecursionError) as error:
            raise StorageError(
                path, f"не удалось прочитать JSON ({type(error).__name__})"
            ) from None
        return data

    def _read_records(self, filename: str) -> list[dict]:
        """Прочитать JSON со списком записей пользователей или портфелей."""
        data = self._read_json(filename)
        self._validate_records(data, self._path(filename))
        return data

    def _user_records(self, users: list[User]) -> list[dict]:
        """Получить JSON-представление переданных объектов пользователей."""
        path = self._path(USERS_FILE)
        if not isinstance(users, list) or not all(
            isinstance(user, User) for user in users
        ):
            raise StorageError(path, "ожидается список объектов User")
        try:
            return [user.to_dict() for user in users]
        except (AttributeError, TypeError, ValueError) as error:
            raise StorageError(
                path, f"некорректные поля пользователя ({type(error).__name__})"
            ) from None

    def _decode_users(self, records: list[dict]) -> list[User]:
        """Восстановить пользователей и проверить уникальность ID и имён."""
        path = self._path(USERS_FILE)
        self._validate_records(records, path)
        users = []
        seen_ids = set()
        seen_names = set()
        for index, record in enumerate(records, start=1):
            try:
                user = User.from_dict(record)
            except (KeyError, TypeError, ValueError, AttributeError):
                raise StorageError(
                    path, f"некорректные поля пользователя в записи {index}"
                ) from None
            if user.user_id in seen_ids:
                raise StorageError(path, f"повторяется user_id в записи {index}")
            if user.username in seen_names:
                raise StorageError(path, f"повторяется username в записи {index}")
            seen_ids.add(user.user_id)
            seen_names.add(user.username)
            users.append(user)
        return users

    def _decode_portfolios(
        self, records: list[dict], users: list[User]
    ) -> list[Portfolio]:
        """Проверить записи и связать портфели с переданными объектами User."""
        path = self._path(PORTFOLIOS_FILE)
        self._validate_records(records, path)
        self._decode_users(self._user_records(users))
        owners = {user.user_id: user for user in users}
        portfolios = []
        seen_ids = set()
        for index, record in enumerate(records, start=1):
            try:
                owner = owners[record["user_id"]]
                portfolio = Portfolio.from_dict(record, owner)
            except (KeyError, TypeError, ValueError, AttributeError):
                raise StorageError(
                    path,
                    f"некорректные поля или отсутствует владелец портфеля "
                    f"в записи {index}",
                ) from None
            if portfolio.user_id in seen_ids:
                raise StorageError(path, f"повторяется портфель в записи {index}")
            seen_ids.add(portfolio.user_id)
            portfolios.append(portfolio)
        return portfolios

    def _write_json(self, filename: str, data: object) -> None:
        """Сериализовать данные и атомарно заменить файл из того же каталога."""
        path = self._path(filename)
        temporary_path = None
        try:
            text = json.dumps(data, ensure_ascii=False, indent=4, allow_nan=False)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.data_dir,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary_path = Path(file.name)
                file.write(text + "\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        except (OSError, UnicodeError, TypeError, ValueError, RecursionError) as error:
            raise StorageError(
                path, f"не удалось сохранить JSON ({type(error).__name__})"
            ) from None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    # Отказ очистки не должен скрыть исходную ошибку записи.
                    pass

    def load_users(self) -> list[User]:
        """Прочитать и проверить пользователей, не изменяя файлы данных."""
        return self._decode_users(self._read_records(USERS_FILE))

    def save_users(self, users: list[User]) -> None:
        """Сохранить пользователей, не оставляя существующие портфели без владельцев."""
        records = self._user_records(users)
        validated_users = self._decode_users(records)
        previous_users = self.load_users()
        previous_portfolios = self.load_portfolios(previous_users)
        owner_ids = {user.user_id for user in validated_users}
        if any(portfolio.user_id not in owner_ids for portfolio in previous_portfolios):
            raise StorageError(
                self._path(USERS_FILE),
                "новый список пользователей оставляет портфель без владельца",
            )
        self._write_json(USERS_FILE, records)

    def load_portfolios(self, users: list[User] | None = None) -> list[Portfolio]:
        """Связать портфели с переданными User, иначе загрузить их из users.json."""
        if users is None:
            users = self.load_users()
        return self._decode_portfolios(self._read_records(PORTFOLIOS_FILE), users)

    def save_portfolios(self, portfolios: list[Portfolio]) -> None:
        """Сохранить портфели существующих пользователей после проверки всех записей."""
        path = self._path(PORTFOLIOS_FILE)
        if not isinstance(portfolios, list) or not all(
            isinstance(portfolio, Portfolio) for portfolio in portfolios
        ):
            raise StorageError(path, "ожидается список объектов Portfolio")
        try:
            records = [portfolio.to_dict() for portfolio in portfolios]
        except (AttributeError, TypeError, ValueError) as error:
            raise StorageError(
                path, f"некорректные поля портфеля ({type(error).__name__})"
            ) from None
        users = self.load_users()
        self.load_portfolios(users)
        self._decode_portfolios(records, users)
        self._write_json(PORTFOLIOS_FILE, records)

    def _validate_rates(self, snapshot: object) -> None:
        """Проверить схему снимка курсов, не удаляя дополнительные метаданные."""
        path = self._path(RATES_FILE)
        if (
            not isinstance(snapshot, dict)
            or not isinstance(snapshot.get("pairs"), dict)
            or "last_refresh" not in snapshot
        ):
            raise StorageError(path, "ожидается объект с pairs и last_refresh")

        try:
            if snapshot["last_refresh"] is not None:
                parse_timestamp(snapshot["last_refresh"])
            for pair, quote in snapshot["pairs"].items():
                if not isinstance(pair, str) or not isinstance(quote, dict):
                    raise ValueError("Некорректная запись котировки")
                codes = pair.split("_")
                if len(codes) != 2 or any(
                    validate_currency_code(code) != code for code in codes
                ):
                    raise ValueError("Некорректный код валютной пары")
                validate_number(quote["rate"], "Курс")
                parse_timestamp(quote["updated_at"])
                if not isinstance(quote["source"], str) or not quote["source"].strip():
                    raise ValueError("Не указан источник котировки")
        except (KeyError, TypeError, ValueError):
            raise StorageError(
                path, "некорректная схема котировки или времени обновления"
            ) from None

    def load_rates(self) -> dict:
        """Прочитать проверенный снимок котировок вместе с его метаданными."""
        snapshot = self._read_json(RATES_FILE)
        self._validate_rates(snapshot)
        return snapshot

    def save_rates(self, snapshot: dict) -> None:
        """Сохранить корректный снимок, отказавшись менять повреждённый файл."""
        self._validate_rates(snapshot)
        self.load_rates()
        self._write_json(RATES_FILE, snapshot)


class DatabaseManager(JsonStorage):
    """Единый настроенный JSON-репозиторий для работающего приложения."""

    _instance = None

    def __new__(cls, settings: SettingsLoader | None = None):
        """Выбрать простой __new__, сохранив JsonStorage обычным классом."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, settings: SettingsLoader | None = None) -> None:
        """Один раз создать хранилище по проверенным настройкам приложения."""
        if settings is None:
            settings = SettingsLoader()
        if not isinstance(settings, SettingsLoader):
            raise ConfigurationError(
                DEFAULT_CONFIG_FILE, "ожидается объект SettingsLoader"
            )
        filenames = {key: settings.get(key) for key in JSON_FILE_KEYS}
        signature = (settings.get("data_dir"), *filenames.values())
        if getattr(self, "_initialized", False):
            if signature != self._signature:
                raise ConfigurationError(
                    settings.config_path,
                    "пути хранилища изменились: перезапустите приложение",
                )
            return
        super().__init__(settings.get("data_dir"), **filenames)
        self._signature = signature
        self._initialized = True
