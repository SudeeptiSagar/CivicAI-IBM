"""Process configuration, read from the environment.

Mirrors `.env.example`. Secrets live in the environment and never in the repo.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["BusBackend", "SentinelMode", "Settings", "settings"]

SentinelMode = Literal["strict", "permissive", "shadow"]
BusBackend = Literal["memory", "redis"]


class Settings(BaseSettings):
    """Runtime settings. Every field is CIVICAI_-prefixed in the environment."""

    model_config = SettingsConfigDict(
        env_prefix="CIVICAI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["dev", "ci", "prod"] = "dev"
    log_level: str = "INFO"

    # Sentinel (PRD section 8.3)
    sentinel_mode: SentinelMode = "strict"
    sentinel_deadline_ms: int = Field(default=2000, gt=0)
    #: Whether `agents.base.Agent` waits for a Sentinel verdict before calling
    #: `handle()` (the "verdict gate", P4). Defaults to False so every existing
    #: test and deployment keeps its current behaviour — Sentinel verifying
    #: alongside consumers, not in front of them — unless explicitly turned on.
    #: See `common/verdict_gate.py` and `docs/sentinel.md`.
    sentinel_gate_enabled: bool = False
    #: How often the gate polls for a verdict while waiting, in milliseconds.
    #: Bounded well below the 2s deadline so the gate never busy-polls the bus
    #: or the database; it is a plain sleep loop, not a blocking read.
    sentinel_gate_poll_ms: int = Field(default=50, gt=0)

    # Bus (PRD section 9.1)
    bus_backend: BusBackend = "memory"
    redis_url: str = "redis://localhost:6379/0"

    # State and object store (wired in P1)
    database_url: str = "postgresql://civicai:civicai@localhost:5432/civicai"
    s3_endpoint: str = "http://localhost:9000"
    s3_bucket: str = "civicai-media"
    s3_access_key: str = ""
    s3_secret_key: str = ""

    # LLM provider (PRD section 6.2). Empty means unconfigured: agents that need
    # reasoning must fail loudly rather than invent output.
    llm_provider: str = ""
    watsonx_api_key: str = ""
    watsonx_project_id: str = ""
    watsonx_url: str = ""

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_provider)


@lru_cache(maxsize=1)
def settings() -> Settings:
    """Process-wide settings, read once."""
    return Settings()
