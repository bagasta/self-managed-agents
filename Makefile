.PHONY: help install dev db-up migrate upgrade downgrade lint format wa wa-build dev-all

help:
	@echo "Managed Agent Platform"
	@echo ""
	@echo "  make install       Install Python dependencies"
	@echo "  make dev           Run API in dev mode (uvicorn --reload)"
	@echo "  make wa            Run WhatsApp Go microservice (port 8080)"
	@echo "  make wa-build      Build wa-service binary"
	@echo "  make dev-all       Run API + wa-service (2 terminals needed)"
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

wa:
	cd wa-service && PYTHON_WEBHOOK_URL=http://localhost:8000/v1/channels/wa/incoming ./wa-service

wa-build:
	cd wa-service && go build -o wa-service .

dev-all:
	@echo "=== Jalankan di 2 terminal terpisah ==="
	@echo ""
	@echo "Terminal 1 (Python API):"
	@echo "  make dev"
	@echo ""
	@echo "Terminal 2 (WhatsApp Service):"
	@echo "  make wa"

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
