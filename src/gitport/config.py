"""Runtime configuration. Everything is overridable via env or .env file."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """gitport settings.

    All fields read from the environment (or .env) with the GITPORT_ prefix,
    except ``cohere_api_key`` which reads COHERE_API_KEY.
    """

    model_config = SettingsConfigDict(env_prefix="GITPORT_", env_file=".env", extra="ignore")

    cohere_api_key: str = ""

    chat_model: str = "command-a-plus-05-2026"
    embed_model: str = "embed-v4.0"
    rerank_model: str = "rerank-v3.5"

    index_path: str = ".gitport/index.sqlite3"
    rules_dir: str = "docs/rules"

    top_k_rules: int = 3
    embed_candidate_k: int = 25
    chunk_size: int = 1200
    chunk_overlap: int = 150

    max_diff_chars: int = 60_000
    agent_max_steps: int = 8
    agent_temperature: float = 0.2

    api_token: str = ""
    fail_open: bool = False

    # policy + reporting
    policy_path: str = ".gitport/policy.toml"
    reports_db: str = ".gitport/reports.sqlite3"
    store_reports: bool = True

    # API server hardening
    rate_limit_rpm: int = 120          # requests per minute per client, 0 disables
    max_request_bytes: int = 5_000_000
    cors_origins: str = ""             # comma-separated, empty = same-origin only
    log_format: str = "text"           # text | json


def get_settings() -> Settings:
    return Settings()
