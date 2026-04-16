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

    # Sandbox
    sandbox_base_dir: str = "/tmp/agent-sandboxes"
    docker_sandbox_image: str = "python:3.12-slim"
    docker_host: str = "unix:///run/docker.sock"

    # Agent limits
    agent_max_steps: int = 12
    agent_timeout_seconds: int = 300

    # Logging
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
