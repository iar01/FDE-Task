"""Settings from environment variables (or a local .env, never committed). Names are in .env.example."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.clock import Clock, FixedClock, SystemClock


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    reporting_dir: Path = Path("data/reporting_db")
    drafts_path: Path = Path("data/draft_quotes.json")

    # 127.0.0.1, not localhost: on Windows "localhost" tries ::1 first and the mock binds IPv4 only (~2 s per request)
    stock_api_url: str = "http://127.0.0.1:8080"
    stock_timeout_seconds: float = 2.0
    stock_max_attempts: int = 4
    stock_backoff_base_seconds: float = 0.5
    stock_max_backoff_seconds: float = 5.0

    app_db_path: Path = Path("app.db")
    rules_path: Path = Path("rules/approval_rules.yaml")
    outputs_dir: Path = Path("outputs")

    # Set for the sample run / demos, e.g. 2026-10-12T09:30:00-05:00. Unset = real time.
    clock_override: datetime | None = None

    @field_validator("clock_override")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("CLOCK_OVERRIDE must include a UTC offset")
        return v

    def clock(self) -> Clock:
        return FixedClock(self.clock_override) if self.clock_override else SystemClock()
