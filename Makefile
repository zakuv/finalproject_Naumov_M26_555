.PHONY: install run project lint test format build publish package-install

install:
	uv sync --locked

run:
	uv run valutatrade

project:
	uv run project

lint:
	uv run ruff check .

test:
	uv run python -m unittest discover -s tests -v

format:
	uv run ruff format .

build:
	uv build

publish:
	uvx twine check dist/*

package-install: install build
	uv pip install --python .venv/bin/python --reinstall --no-deps dist/*.whl
