.PHONY: help install dev db-up migrate upgrade downgrade lint format wa wa-build dev-all wa-dev-build wa-dev sandbox-build sandbox-check seed-agents deploy-api-fast deploy-app deploy-all mcp-smoke-live mcp-smoke-live-strict mcp-smoke-live-reauth mcp-smoke-live-onboard

PROD_COMPOSE := docker compose -f deploy/docker-compose.prod.yml

help:
	@echo "Managed Agent Platform"
	@echo ""
	@echo "  make install                Install Python dependencies"
	@echo "  make dev                    Run API in dev mode (uvicorn --reload)"
	@echo "  make wa                     Run WhatsApp Go microservice (port 8080)"
	@echo "  make wa-build               Build wa-service binary"
	@echo "  make wa-dev-build           Build wa-dev-service binary"
	@echo "  make wa-dev                 Run WA dev number service (port 8081) + dashboard"
	@echo "  make sandbox-build          Build Docker sandbox image for file/subagent tools"
	@echo "  make sandbox-check          Verify Docker sandbox image exists locally"
	@echo "  make deploy-api-fast        Rebuild/restart only the API"
	@echo "  make deploy-app             Build shared app image; restart API + scheduler"
	@echo "  make deploy-all             Rebuild/restart the full production stack"
	@echo "  make dev-all                Run API + wa-service (2 terminals needed)"
	@echo "  make db-up                  Start PostgreSQL via docker-compose"
	@echo "  make migrate                Generate migration  (MSG='description')"
	@echo "  make upgrade                Apply all pending migrations"
	@echo "  make downgrade              Rollback one migration"
	@echo "  make lint                   Run ruff linter"
	@echo "  make format                 Run ruff formatter"
	@echo "  make seed-agents            Seed system sub-agents to DB"
	@echo "  make mcp-smoke-live         Run safe live Google MCP smoke suite"
	@echo "  make mcp-smoke-live-strict  Run live Google MCP smoke suite in strict mode"
	@echo "  make mcp-smoke-live-reauth  Generate fresh Google re-auth link for smoke testing"
	@echo "  make mcp-smoke-live-onboard Show tester steps for re-auth + smoke test"

install:
	pip install -r requirements.txt

dev:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

wa: wa-build
	cd wa-service && PYTHON_WEBHOOK_URL=http://localhost:8000/v1/channels/wa/incoming ./wa-service

wa-build:
	cd wa-service && go build -o wa-service .

wa-dev-build:
	cd wa-dev-service && go build -o wa-dev-service .

wa-dev: wa-dev-build
	cd wa-dev-service && set -a && . ../.env && set +a && MAIN_API_KEY=$$API_KEY MAIN_API_URL=http://localhost:8000 ./wa-dev-service

sandbox-build:
	docker build -f sandbox.Dockerfile -t $${DOCKER_SANDBOX_IMAGE:-managed-agents-sandbox:latest} .

sandbox-check:
	docker image inspect $${DOCKER_SANDBOX_IMAGE:-managed-agents-sandbox:latest} >/dev/null

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

seed-agents:
	python -m scripts.seed_system_agents

deploy-api-fast:
	$(PROD_COMPOSE) build api
	$(PROD_COMPOSE) up -d --no-deps api

deploy-app:
	$(PROD_COMPOSE) build api
	$(PROD_COMPOSE) up -d --no-deps api scheduler

deploy-all:
	$(PROD_COMPOSE) up -d --build

mcp-smoke-live:
	RUN_GOOGLE_MCP_LIVE_SMOKE=true \
	GOOGLE_MCP_INTEGRATION_URL=$${GOOGLE_MCP_INTEGRATION_URL:-http://localhost:8003} \
	GOOGLE_MCP_URL=$${GOOGLE_MCP_URL:-http://localhost:8002/mcp} \
	GOOGLE_MCP_EXTERNAL_USER_ID=$${GOOGLE_MCP_EXTERNAL_USER_ID:-62895619356936} \
	GOOGLE_MCP_AGENT_ID=$${GOOGLE_MCP_AGENT_ID:-46ed1c39-c343-4d42-a5ff-2559f43efa0e} \
	/home/bagas/managed-agents-project/.venv/bin/python -m pytest -q tests/test_google_mcp_live_smoke.py

mcp-smoke-live-strict:
	RUN_GOOGLE_MCP_LIVE_SMOKE=true \
	GOOGLE_MCP_LIVE_SMOKE_STRICT=true \
	GOOGLE_MCP_INTEGRATION_URL=$${GOOGLE_MCP_INTEGRATION_URL:-http://localhost:8003} \
	GOOGLE_MCP_URL=$${GOOGLE_MCP_URL:-http://localhost:8002/mcp} \
	GOOGLE_MCP_EXTERNAL_USER_ID=$${GOOGLE_MCP_EXTERNAL_USER_ID:-62895619356936} \
	GOOGLE_MCP_AGENT_ID=$${GOOGLE_MCP_AGENT_ID:-46ed1c39-c343-4d42-a5ff-2559f43efa0e} \
	/home/bagas/managed-agents-project/.venv/bin/python -m pytest -q tests/test_google_mcp_live_smoke.py


mcp-smoke-live-reauth:
	GOOGLE_MCP_INTEGRATION_URL=$${GOOGLE_MCP_INTEGRATION_URL:-http://localhost:8003} \
	GOOGLE_MCP_EXTERNAL_USER_ID=$${GOOGLE_MCP_EXTERNAL_USER_ID:-62895619356936} \
	GOOGLE_MCP_AGENT_ID=$${GOOGLE_MCP_AGENT_ID:-46ed1c39-c343-4d42-a5ff-2559f43efa0e} \
	/home/bagas/managed-agents-project/.venv/bin/python scripts/generate_google_mcp_reauth_link.py


mcp-smoke-live-onboard:
	@echo "Google MCP smoke onboarding:"
	@echo "1) Generate fresh link: make mcp-smoke-live-reauth"
	@echo "2) Open auth_url and finish Google consent"
	@echo "3) Run smoke suite: make mcp-smoke-live"
	@echo "4) For stricter gating: make mcp-smoke-live-strict"
	@echo ""
	@echo "Optional env overrides:"
	@echo "  GOOGLE_MCP_EXTERNAL_USER_ID=<user>"
	@echo "  GOOGLE_MCP_AGENT_ID=<agent-id>"
	@echo "  GOOGLE_MCP_INTEGRATION_URL=http://localhost:8003"
	@echo "  GOOGLE_MCP_URL=http://localhost:8002/mcp"
