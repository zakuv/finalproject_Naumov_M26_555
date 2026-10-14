# ValutaTrade Hub

Учебное консольное приложение для симуляции валютного кошелька:
регистрация пользователей, управление портфелем, покупка и продажа валют.
Отдельный Parser Service будет получать фиатные и криптовалютные курсы
для локального кэша и истории измерений.

Текущий этап — подготовка проекта. Созданы структура модулей, пакет,
команды запуска, конфигурация Ruff и сборка. При запуске выводится
`ValutaTrade Hub`. Модели и пользовательские команды реализуются
на следующих этапах.

## Требования

- Python 3.12 или новее.
- uv 0.12.18 или новее.
- Make для сокращённых команд; также можно использовать uv напрямую.

## Установка и запуск

В каталоге проекта:

```bash
make install
make run
```

Без Make:

```bash
uv sync --locked
uv run valutatrade
```

Также доступны `make project`, `uv run project` и `uv run python main.py`.
Версии зависимостей зафиксированы в `uv.lock`. PrettyTable установлен
для вывода таблиц; Ruff находится в группе зависимостей для разработки.

## Структура

```text
finalproject_Naumov_M26_555/
├── data/
│   ├── users.json
│   ├── portfolios.json
│   ├── rates.json
│   └── exchange_rates.json
├── valutatrade_hub/
│   ├── __init__.py
│   ├── logging_config.py
│   ├── decorators.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── models.py
│   │   ├── currencies.py
│   │   ├── exceptions.py
│   │   ├── usecases.py
│   │   └── utils.py
│   ├── infra/
│   │   ├── __init__.py
│   │   ├── settings.py
│   │   └── database.py
│   ├── parser_service/
│   │   ├── __init__.py
│   │   ├── config.py
│   │   ├── api_clients.py
│   │   ├── updater.py
│   │   ├── storage.py
│   │   └── scheduler.py
│   └── cli/
│       ├── __init__.py
│       └── interface.py
├── main.py
├── Makefile
├── pyproject.toml
├── uv.lock
├── README.md
└── .gitignore
```

`core` содержит бизнес-логику, `infra` — настройки и доступ к данным,
`parser_service` — получение курсов, `cli` — ввод команд и вывод результатов.
Пакет находится непосредственно в корне проекта, как в задании.

JSON-файлы из `data/` содержат рабочие данные и исключены из Git.
В стартовом архиве они пустые. Директория отслеживается через `.gitkeep`;
автоматическая инициализация JSON-хранилища будет добавлена вместе с моделями.
Логи, виртуальное окружение, сборки, временные файлы и файлы с ключами
также исключены из Git.

## Проверка и сборка

```bash
make lint
uv run ruff format --check .
make build
make publish
```

`make format` форматирует код. `make build` создаёт wheel и исходный архив
в `dist/`. `make publish` проверяет метаданные сборок через Twine без
отправки на PyPI. `make package-install` сначала устанавливает зависимости
из `uv.lock`, затем заменяет пакет собранным wheel в окружении `.venv`.

Для проверки запуска установленного пакета:

```bash
make package-install
.venv/bin/valutatrade
```

## Следующие этапы

1. Модели User, Wallet и Portfolio, JSON-хранилище.
2. Команды register, login, show-portfolio, buy, sell и get-rate.
3. Валюты, доменные исключения, настройки и журналирование.
4. Parser Service, update-rates, show-rates и история курсов.
5. Проверки всего проекта, итоговая документация и демонстрация.
