from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://postgres:password@localhost:5432/managed_agents"

    # Auth
    api_key: str = "change-me"

    # OpenRouter
    openrouter_api_key: str = ""

    # Mistral (used for PDF OCR)
    mistral_api_key: str = ""

    # Sandbox
    sandbox_base_dir: str = "/tmp/agent-sandboxes"
    docker_sandbox_image: str = "python:3.12"
    docker_host: str = "unix:///run/docker.sock"

    # Agent limits
    agent_max_steps: int = 12
    agent_timeout_seconds: int = 300

    # Memory
    short_term_memory_turns: int = 10   # conversation turns kept in LLM context
    ltm_extraction_every: int = 10      # extract LTM every N user messages

    # WhatsApp microservice
    wa_service_url: str = "http://localhost:8080"
    wa_dev_service_url: str = "http://localhost:8081"

    # Logging
    log_level: str = "INFO"

    # CORS — comma-separated or JSON list; default allows all (dev only)
    allowed_origins: list[str] = ["*"]

    # Developer notification phone (WhatsApp) for error alerts
    developer_phone: str = ""

    # Error tracking
    sentry_dsn: str = ""
    environment: str = "development"

    # Redis — dipakai untuk event bus multi-process dan rate limiting
    # Set ke "" untuk disable Redis (fallback ke in-memory, single-worker only)
    redis_url: str = ""

    # Tunable limits
    context_summary_trigger: int = 10      # summarize after N user messages
    default_subagent_model: str = "openai/gpt-4o-mini"
    default_subagent_max_tokens: int = 4096
    media_doc_max_chars: int = 12000
    llm_max_tokens: int = 4096
    message_max_length: int = 10_000       # max chars per user message
    media_max_length: int = 10_000_000     # max chars for base64 media payload
    max_concurrent_sandboxes: int = 10     # max Docker sandbox containers running simultaneously


@lru_cache
def get_settings() -> Settings:
    return Settings()
