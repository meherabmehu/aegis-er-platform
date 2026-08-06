.PHONY: help install dev test run lint load reset

help:
	@echo "AEGIS-ER — available targets:"
	@echo "  make install     Install the aegis-core library and API dependencies"
	@echo "  make dev         Run the API in dev mode (reload)"
	@echo "  make test        Run unit tests"
	@echo "  make run         Run the API + dashboard on port 8000"
	@echo "  make lint        Run ruff (if installed)"
	@echo "  make load        Run pure-python load test"
	@echo "  make reset       Reset all caches/build artifacts"

install:
	pip install -e libs/aegis
	pip install -r services/assignment-solver/requirements.txt

dev:
	cd services/assignment-solver && PYTHONPATH=../../libs/aegis AEGIS_DASHBOARD_DIR=../dashboard uvicorn app:app --reload --host 0.0.0.0 --port 8000

run:
	cd services/assignment-solver && PYTHONPATH=../../libs/aegis AEGIS_DASHBOARD_DIR=../dashboard AEGIS_SIMULATOR=true python app.py

test:
	PYTHONPATH=libs/aegis pytest libs/aegis/tests -q

lint:
	@echo "Running ruff (if installed)..."
	-ruff check libs/aegis/aegis services/assignment-solver

load:
	PYTHONPATH=libs/aegis python test/load/load_test.py

reset:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf .coverage htmlcov
