from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://postgres:password@localhost:5432/managed_agents"
    db_pool_size: int = 16
    db_max_overflow: int = 16
    db_pool_timeout_seconds: float = 10.0
    db_pool_recycle_seconds: int = 1800

    # Auth
    api_key: str = "change-me"

    # OpenRouter
    openrouter_api_key: str = ""

    # Mistral (used for PDF and Office document extraction)
    mistral_api_key: str = ""

    # Arthur content-aware model routing
    arthur_primary_model: str = "deepseek/deepseek-v4-flash"
    arthur_document_model: str = "mistral-ocr-latest"
    arthur_image_model: str = "openai/gpt-4.1-mini"
    arthur_engine_version: str = "arthur-progressive-v1"
    arthur_prompt_version: str = "arthur-kernel-v7"
    llm_request_timeout_seconds: float = 120.0
    llm_max_retries: int = 1

    # Tavily (web search / browsing for agents)
    tavily_api_key: str = ""
    tavily_force_ipv4: bool = True

    # Sandbox
    sandbox_base_dir: str = "/tmp/agent-sandboxes"
    # Host-side path of sandbox_base_dir. When the app runs inside a container and
    # spawns sibling sandbox containers via the mounted Docker socket, bind-mount
    # sources are resolved by the HOST daemon, not the app container filesystem.
    # Set this to the host path that backs sandbox_base_dir so file ops (app-side)
    # and execute()/deploy (sibling container) target the same directory.
    # Empty string => same as sandbox_base_dir (dev / app-on-host: no translation).
    sandbox_host_base_dir: str = ""
    docker_sandbox_image: str = "managed-agents-sandbox:latest"
    docker_host: str = "unix:///run/docker.sock"
    sandbox_subagents_enabled: bool = True  # re-enabled after VPS DinD path + stability fixes
    # Per-container resource caps (env-configurable for different VPS sizes)
    sandbox_mem_limit: str = "1g"
    sandbox_nano_cpus: int = 1_000_000_000  # 1.0 CPU core
    # Orphan cleanup TTLs (seconds)
    sandbox_container_ttl_seconds: int = 900   # kill labeled containers older than this
    sandbox_workspace_ttl_seconds: int = 86400  # remove workspace dirs idle longer than this

    # Agent limits
    agent_max_steps: int = 12
    agent_timeout_seconds: int = 300

    # Memory
    short_term_memory_turns: int = 20   # conversation turns kept in LLM context
    ltm_extraction_every: int = 5       # extract LTM every N user messages

    # WhatsApp microservice
    wa_service_url: str = "http://localhost:8080"
    wa_dev_service_url: str = "http://localhost:8081"
    wa_dev_public_phone: str = ""
    wa_dev_public_name: str = "Arthur AI Dev"

    # Google Workspace MCP runtime routing
    workspace_mcp_url: str = ""
    workspace_mcp_runtime_url: str = ""
    workspace_mcp_url_local: str = ""
    workspace_mcp_prefer_local: str = "false"
    workspace_mcp_token: str = ""
    google_integration_service_url: str = ""

    # Logging
    log_level: str = "INFO"

    # CORS — comma-separated or JSON list; default allows all (dev only)
    allowed_origins: list[str] = ["*"]

    # Developer notification phone (WhatsApp) for error alerts
    developer_phone: str = ""

    # Error tracking
    sentry_dsn: str = ""
    environment: str = "development"
    app_commit_sha: str = "unknown"

    # Redis — dipakai untuk event bus multi-process dan rate limiting
    # Set ke "" untuk disable Redis (fallback ke in-memory, single-worker only)
    redis_url: str = ""
    redis_max_connections: int = 64
    redis_pool_timeout_seconds: float = 5.0
    redis_socket_connect_timeout_seconds: float = 3.0
    redis_socket_timeout_seconds: float = 10.0
    redis_health_check_interval_seconds: int = 30

    # Tunable limits
    context_summary_trigger: int = 10      # summarize after N user messages
    default_subagent_model: str = "openai/gpt-4o-mini"
    default_subagent_max_tokens: int = 8192
    media_doc_max_chars: int = 12000
    llm_max_tokens: int = 1024
    message_max_length: int = 10_000       # max chars per user message
    media_max_length: int = 10_000_000     # max chars for base64 media payload
    max_concurrent_sandboxes: int = 6      # bounded semaphore; requests queue instead of failing
    max_concurrent_agent_runs: int = 24    # global per-process backpressure for burst traffic
    embedded_scheduler_enabled: bool = True  # false when a dedicated scheduler container is running


@lru_cache
def get_settings() -> Settings:
    return Settings()
