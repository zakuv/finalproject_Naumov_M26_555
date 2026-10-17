"""Единый экземпляр настроек из секции [tool.valutatrade] в TOML."""

import math
import tomllib
from pathlib import Path

from valutatrade_hub.core.exceptions import ConfigurationError
from valutatrade_hub.core.utils import DEFAULT_BASE_CURRENCY, RATES_TTL_SECONDS

DEFAULT_CONFIG_FILE = "pyproject.toml"
DEFAULT_SETTINGS = {
    "data_dir": "data",
    "users_file": "users.json",
    "portfolios_file": "portfolios.json",
    "rates_file": "rates.json",
    "history_file": "exchange_rates.json",
    "rates_ttl_seconds": RATES_TTL_SECONDS,
    "default_base_currency": DEFAULT_BASE_CURRENCY,
    "log_file": "logs/actions.log",
    "log_level": "INFO",
    "log_format": "json",
    "log_max_bytes": 1_048_576,
    "log_backup_count": 3,
}
JSON_FILE_KEYS = ("users_file", "portfolios_file", "rates_file", "history_file")
LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "NOTSET"}


class SettingsLoader:
    """Кешировать проверенную конфигурацию для всех частей приложения."""

    _instance = None

    def __new__(cls, config_path: str | Path | None = None):
        """Использовать __new__: один экземпляр без дополнительного метакласса."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config_path: str | Path | None = None) -> None:
        """Загрузить файл один раз; повторный вызов не перечитывает настройки."""
        if getattr(self, "_initialized", False):
            if config_path is not None and self._resolve_path(config_path) != (
                self._config_path
            ):
                raise ConfigurationError(
                    self._config_path,
                    "для смены файла настроек используйте reload(config_path)",
                )
            return
        self.reload(config_path)

    @staticmethod
    def _resolve_path(config_path: str | Path) -> Path:
        """Привести путь к файлу к абсолютному виду."""
        try:
            return Path(config_path).expanduser().resolve()
        except (TypeError, ValueError, OSError):
            raise ConfigurationError(
                DEFAULT_CONFIG_FILE, "некорректный путь к файлу настроек"
            ) from None

    def get(self, key: str, default: object = None) -> object:
        """Вернуть настройку; имена ключей допускают любой регистр."""
        return self._settings.get(key.lower(), default)

    @property
    def config_path(self) -> Path:
        """Вернуть путь к файлу, из которого загружен текущий кеш."""
        return self._config_path

    def reload(self, config_path: str | Path | None = None) -> None:
        """Заменить кеш только после успешного чтения и проверки всех настроек."""
        if config_path is not None:
            path = self._resolve_path(config_path)
            explicit = True
        elif getattr(self, "_initialized", False):
            path = self._config_path
            explicit = self._explicit_config
        else:
            path = self._resolve_path(DEFAULT_CONFIG_FILE)
            explicit = False

        try:
            with path.open("rb") as file:
                document = tomllib.load(file)
        except FileNotFoundError:
            if explicit:
                raise ConfigurationError(path, "файл настроек не найден") from None
            document = {}
        except (OSError, ValueError) as error:
            raise ConfigurationError(
                path, f"не удалось прочитать TOML ({type(error).__name__})"
            ) from None

        tool = document.get("tool", {})
        section = tool.get("valutatrade", {}) if isinstance(tool, dict) else None
        if not isinstance(section, dict):
            raise ConfigurationError(path, "ожидается секция [tool.valutatrade]")
        if section.keys() - DEFAULT_SETTINGS.keys():
            raise ConfigurationError(path, "неизвестные параметры [tool.valutatrade]")
        settings = DEFAULT_SETTINGS | section
        self._validate_settings(settings, path)
        self._settings = settings
        self._config_path = path
        self._explicit_config = explicit
        self._initialized = True

    @staticmethod
    def _validate_settings(settings: dict, path: Path) -> None:
        """Проверить типы, имена файлов и значения до публикации нового кеша."""
        for key in ("data_dir", "log_file"):
            value = settings[key]
            if not isinstance(value, str) or not value.strip():
                raise ConfigurationError(path, f"{key}: ожидается непустой путь")
            try:
                settings[key] = (path.parent / Path(value).expanduser()).resolve()
            except (ValueError, OSError):
                raise ConfigurationError(path, f"{key}: некорректный путь") from None

        filenames = []
        for key in JSON_FILE_KEYS:
            value = settings[key]
            if (
                not isinstance(value, str)
                or not value.strip()
                or value in {".", ".."}
                or "/" in value
                or "\\" in value
                or "\x00" in value
                or not value.endswith(".json")
            ):
                raise ConfigurationError(path, f"{key}: ожидается имя JSON-файла")
            filenames.append(value.casefold())
        if len(set(filenames)) != len(filenames):
            raise ConfigurationError(path, "имена JSON-файлов должны различаться")

        try:
            protected_paths = {str(path).casefold()}
            protected_paths.update(
                str((settings["data_dir"] / settings[key]).resolve()).casefold()
                for key in JSON_FILE_KEYS
            )
        except (ValueError, OSError):
            raise ConfigurationError(path, "некорректные пути JSON-файлов") from None
        if str(settings["log_file"]).casefold() in protected_paths:
            raise ConfigurationError(
                path, "log_file не должен совпадать с конфигурацией или данными"
            )

        ttl = settings["rates_ttl_seconds"]
        try:
            valid_ttl = type(ttl) in (int, float) and math.isfinite(ttl) and ttl > 0
        except OverflowError:
            valid_ttl = False
        if not valid_ttl:
            raise ConfigurationError(path, "rates_ttl_seconds: нужно число больше 0")
        for key, minimum in (("log_max_bytes", 1), ("log_backup_count", 0)):
            if type(settings[key]) is not int or settings[key] < minimum:
                raise ConfigurationError(path, f"{key}: некорректное целое число")
        if (
            not isinstance(settings["log_level"], str)
            or settings["log_level"] not in LOG_LEVELS
        ):
            raise ConfigurationError(path, "log_level: неизвестный уровень логов")
        if settings["log_format"] != "json":
            raise ConfigurationError(path, "log_format: поддерживается только json")

        from valutatrade_hub.core.currencies import get_currency

        try:
            settings["default_base_currency"] = get_currency(
                settings["default_base_currency"]
            ).code
        except ValueError:
            raise ConfigurationError(
                path, "default_base_currency: неизвестная базовая валюта"
            ) from None
