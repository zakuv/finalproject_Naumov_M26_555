"""Сценарии регистрации, входа и операций с валютами."""

from valutatrade_hub.core.exceptions import StorageError
from valutatrade_hub.core.models import Portfolio, User
from valutatrade_hub.core.utils import validate_password, validate_username
from valutatrade_hub.infra.database import JsonStorage


class UserService:
    """Регистрировать пользователей и хранить текущую сессию в памяти."""

    def __init__(self, storage: JsonStorage) -> None:
        """Создать независимый сервис без активной сессии."""
        self._storage = storage
        self._current_user = None

    @property
    def current_user(self) -> User | None:
        """Вернуть пользователя текущей сессии без возможности её присвоения."""
        return self._current_user

    def register(self, username: str, password: str) -> User:
        """Сохранить нового пользователя и пустой портфель без автоматического входа."""
        username = validate_username(username)
        password = validate_password(password)
        users = self._storage.load_users()
        portfolios = self._storage.load_portfolios(users)
        if any(user.username == username for user in users):
            raise ValueError(f"Имя пользователя '{username}' уже занято")

        next_user_id = max((user.user_id for user in users), default=0) + 1
        user = User.create(next_user_id, username, password)
        new_users = [*users, user]
        new_portfolios = [*portfolios, Portfolio(user)]
        self._storage.save_users(new_users)
        try:
            self._storage.save_portfolios(new_portfolios)
        except StorageError as registration_error:
            try:
                self._storage.save_users(users)
            except StorageError as rollback_error:
                raise StorageError(
                    registration_error.path,
                    "Регистрация не завершена: не удалось сохранить портфель; "
                    f"отмена изменения '{rollback_error.path}' также не удалась. "
                    "Проверьте файлы пользователей и портфелей.",
                ) from None
            raise
        return user

    def login(self, username: str, password: str) -> User:
        """Проверить учётные данные и заменить сессию только после успешного входа."""
        username = validate_username(username)
        users = self._storage.load_users()
        user = next((user for user in users if user.username == username), None)
        if user is None:
            raise ValueError(f"Пользователь '{username}' не найден")
        if not user.verify_password(password):
            raise ValueError("Неверный пароль")
        self._current_user = user
        return user

    def logout(self) -> None:
        """Завершить текущую сессию, не изменяя файлы данных."""
        self._current_user = None

    def require_login(self) -> User:
        """Вернуть вошедшего пользователя или запросить вход через исключение."""
        if self.current_user is None:
            raise PermissionError("Сначала выполните login")
        return self.current_user
