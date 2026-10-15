"""Доступ к JSON-хранилищу пользователей и портфелей."""

import json
import os
import tempfile
from pathlib import Path

from valutatrade_hub.core.exceptions import StorageError
from valutatrade_hub.core.models import Portfolio, User

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

    def __init__(self, data_dir: str | Path = DEFAULT_DATA_DIR) -> None:
        """Запомнить каталог и создать только отсутствующие файлы данных."""
        try:
            self.data_dir = Path(data_dir)
        except (TypeError, ValueError):
            raise StorageError(DEFAULT_DATA_DIR, "некорректный путь каталога") from None
        self.initialize()

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
            path = self.data_dir / filename
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

    def _read_records(self, filename: str) -> list[dict]:
        """Прочитать JSON без замены повреждённых данных пустым списком."""
        path = self.data_dir / filename
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
        self._validate_records(data, path)
        return data

    def _user_records(self, users: list[User]) -> list[dict]:
        """Получить JSON-представление переданных объектов пользователей."""
        path = self.data_dir / USERS_FILE
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
        path = self.data_dir / USERS_FILE
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
        path = self.data_dir / PORTFOLIOS_FILE
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

    def _write_records(self, filename: str, data: list[dict]) -> None:
        """Сериализовать данные и атомарно заменить файл из того же каталога."""
        path = self.data_dir / filename
        temporary_path = None
        try:
            text = json.dumps(data, ensure_ascii=False, indent=4, allow_nan=False)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.data_dir,
                prefix=f".{filename}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary_path = Path(file.name)
                file.write(text + "\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        except (OSError, UnicodeError, TypeError, ValueError) as error:
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
                self.data_dir / USERS_FILE,
                "новый список пользователей оставляет портфель без владельца",
            )
        self._write_records(USERS_FILE, records)

    def load_portfolios(self, users: list[User] | None = None) -> list[Portfolio]:
        """Связать портфели с переданными User, иначе загрузить их из users.json."""
        if users is None:
            users = self.load_users()
        return self._decode_portfolios(self._read_records(PORTFOLIOS_FILE), users)

    def save_portfolios(self, portfolios: list[Portfolio]) -> None:
        """Сохранить портфели существующих пользователей после проверки всех записей."""
        path = self.data_dir / PORTFOLIOS_FILE
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
        self._write_records(PORTFOLIOS_FILE, records)
