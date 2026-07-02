# File Structure

Tanggal snapshot: 2026-07-02

## Root Structure
```text
.
|-- app/                       # FastAPI backend and agent runtime
|-- alembic/                   # DB migrations
|-- tests/                     # pytest suite
|-- wa-service/                # Go WhatsApp production service
|-- wa-dev-service/            # Go shared trial WA service
|-- UI-DEV/                    # static dev UI
|-- docs/                      # active docs and runbooks
|-- Archive/                   # older docs/assets
|-- deploy/                    # production compose and deploy notes
|-- scripts/                   # seed/audit/utility scripts
|-- locust-load/               # load test assets
|-- docker-compose.yml         # local compose
|-- Dockerfile                 # API image
|-- sandbox.Dockerfile         # sandbox image
|-- Makefile                   # local commands
|-- requirements.txt
|-- README.md
|-- CLAUDE.md
```

## Backend Structure
```text
app/
|-- api/                       # FastAPI routers
|-- core/
|   |-- engine/                # agent runtime orchestration
|   |-- domain/                # DB/domain services
|   |-- infra/                 # external adapters: sandbox, WA, Redis, deploy
|   |-- tools/                 # LangChain tool builders
|   |-- utils/                 # phone, logging, sanitizer helpers
|   |-- workers/               # scheduler/event bus
|-- middleware/
|-- models/                    # SQLAlchemy models
|-- schemas/                   # Pydantic schemas
|-- config.py
|-- database.py
|-- main.py
|-- scheduler_worker.py
```

## Frontend Structure
```text
UI-DEV/
|-- index.html
|-- app.js
|-- style.css
|-- test-dashboard.html
|-- test-dashboard.js
|-- test-dashboard.css
```

## WhatsApp Services
```text
wa-service/
|-- main.go
|-- handlers.go
|-- device_manager.go
|-- Dockerfile

wa-dev-service/
|-- main.go
|-- api.go
|-- router.go
|-- store.go
|-- whatsapp.go
|-- router_test.go
|-- Dockerfile
```

## Shared Modules
- Shared Python domain logic is under `app/core/domain`.
- Runtime-specific logic is under `app/core/engine`.
- External adapters are under `app/core/infra`.
- Tool definitions are under `app/core/tools`.
- There is no cross-language shared package between Python and Go services.

## Configuration Files
- `.env.example`: local env template.
- `app/config.py`: Pydantic settings.
- `docker-compose.yml`: local stack.
- `deploy/docker-compose.prod.yml`: production stack.
- `alembic.ini`: migration config.
- `Makefile`: common commands.

## Naming Conventions
- API routers: plural resource names, `app/api/<resource>.py`.
- SQLAlchemy models: singular class, table name plural or established domain name.
- Tool builders: `build_<group>_tools`.
- Tests: `tests/test_<feature>.py`.
- Migration files: ordered numeric/alembic revision prefix plus description.
- Documentation: uppercase architecture docs under `docs/ai-architecture`.

