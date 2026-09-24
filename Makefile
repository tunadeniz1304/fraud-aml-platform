# Anil3 fraud platformu — geliştirici komutları (Linux/macOS/CI; Windows'ta
# aynı komutları doğrudan çalıştırabilirsiniz).

PY ?= python

.PHONY: install lint format typecheck test cov gate data train smoke llm-smoke load run up down

install:
	$(PY) -m pip install -e ".[dev]"

lint:
	ruff check .
	ruff format --check .

format:
	ruff check . --fix
	ruff format .

typecheck:
	mypy app

test:
	$(PY) -m pytest -q

cov:
	$(PY) -m pytest -q --cov=app --cov-report=term --cov-fail-under=80

# Her fazın kalite kapısı (bkz. docs/PLAN.md)
gate: lint typecheck cov
	docker compose build

data:
	$(PY) scripts/generate_synthetic.py --seed 42

train: data
	$(PY) scripts/train_models.py --seed 42

smoke:
	$(PY) scripts/smoke_dashboard.py

llm-smoke:
	$(PY) scripts/llm_smoke.py

load:
	$(PY) scripts/load_test.py --tps 1000 --seconds 10

run:
	$(PY) server.py

up:
	docker compose up --build

down:
	docker compose down
