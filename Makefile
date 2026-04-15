.PHONY: help install dev db-up migrate upgrade downgrade lint format

help:
	@echo "Managed Agent Platform"
	@echo ""
	@echo "  make install       Install Python dependencies"
	@echo "  make dev           Run API in dev mode (uvicorn --reload)"
	@echo "  make db-up         Start PostgreSQL via docker-compose"
	@echo "  make migrate       Generate migration  (MSG='description')"
	@echo "  make upgrade       Apply all pending migrations"
	@echo "  make downgrade     Rollback one migration"
	@echo "  make lint          Run ruff linter"
	@echo "  make format        Run ruff formatter"

install:
	pip install -r requirements.txt

dev:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

db-up:
	docker compose up -d postgres

migrate:
	alembic revision --autogenerate -m "$(MSG)"

upgrade:
	alembic upgrade head

downgrade:
	alembic downgrade -1

lint:
	ruff check app/ alembic/

format:
	ruff format app/ alembic/
