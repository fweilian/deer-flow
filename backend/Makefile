install:
	uv sync

dev:
	PYTHONPATH=. DEERFLOW_GATEWAY_RELOAD=1 uv run python run_gateway.py

gateway:
	PYTHONPATH=. uv run python run_gateway.py

test:
	PYTHONPATH=. uv run pytest tests/ -v

lint:
	uvx ruff check .
	uvx ruff format --check .

format:
	uvx ruff check . --fix && uvx ruff format .
