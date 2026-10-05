"""Typed runtime configuration.

Every environment variable the project reads is declared here with a default, so
``tell/config.py`` is the single source of truth for ``.env.example``. Values come
from the process environment first, then from a ``.env`` file in the working
directory. Secrets are never defaulted; they are ``None`` until provided.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration for the ``tell`` package (see ``docs/SPEC.md`` section 2)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- database -----------------------------------------------------------
    database_url: str = Field(
        default="postgresql+psycopg://karankapur@localhost:5432/tell_dev",
        description="SQLAlchemy URL for the primary Postgres database.",
    )

    # --- outbound identity ---------------------------------------------------
    sec_user_agent: str = Field(
        default="tell/0.1 (contact@example.invalid)",
        description="User-Agent for SEC EDGAR; the SEC requires a real contact address.",
    )
    tell_http_user_agent: str = Field(
        default="tell/0.1 (+https://github.com/karankapur/tell)",
        description="User-Agent sent to every non-SEC source.",
    )

    # --- integrations (secrets stay None unless provided) -------------------
    slack_webhook_url: SecretStr | None = Field(
        default=None, description="Slack incoming webhook for the daily digest."
    )
    github_token: SecretStr | None = Field(
        default=None, description="GitHub token for the GitHub org collector."
    )
    tell_data_repo: str = Field(
        default="karankapur/tell-data",
        description="GitHub repo (owner/name) receiving JSONL events and the static site.",
    )
    tell_data_push_token: SecretStr | None = Field(
        default=None, description="Fine-grained PAT with contents:write on TELL_DATA_REPO."
    )

    # --- politeness budgets --------------------------------------------------
    tell_poll_concurrency: int = Field(default=8, ge=1, description="Parallel board fetches.")
    tell_rate_edgar_rps: float = Field(
        default=5.0, gt=0, description="Requests per second against SEC EDGAR (half the ceiling)."
    )
    tell_rate_news_rps: float = Field(
        default=1.0, gt=0, description="Requests per second against Google News RSS."
    )
    tell_rate_ats_rps: float = Field(
        default=8.0, gt=0, description="Requests per second per ATS vendor host."
    )
    tell_discovery_budget: int = Field(
        default=300, ge=0, description="Max new domains per discovery run."
    )
    tell_discovery_rps: float = Field(
        default=4.0, gt=0, description="Requests per second during ATS discovery."
    )
    tell_http_timeout_s: float = Field(
        default=20.0, gt=0, description="Timeout in seconds for every outbound HTTP request."
    )

    # --- publishing / serving ------------------------------------------------
    tell_base_url: str = Field(
        default="https://karankapur.github.io/tell-data",
        description="Public base URL of the static site and api/v0 JSON.",
    )
    tell_api_key: SecretStr | None = Field(
        default=None, description="Optional API key for the FastAPI service (tell-mcp client)."
    )

    # --- alerting ------------------------------------------------------------
    tell_alert_threshold: float = Field(
        default=6.0, description="Score at which score_crossed_threshold fires (upward)."
    )
    tell_slack_tz: str = Field(
        default="America/Los_Angeles", description="IANA timezone for the Slack digest."
    )
    tell_slack_digest_hour: int = Field(
        default=8, ge=0, le=23, description="Local hour at which the digest run sends."
    )

    # --- review UI -----------------------------------------------------------
    tell_review_password: SecretStr | None = Field(
        default=None, description="Password gating the Streamlit review page."
    )

    # --- environment ---------------------------------------------------------
    tell_env: Literal["dev", "test", "ci", "prod"] = Field(
        default="dev", description="Deployment environment name."
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings`, built once and cached.

    Tests that need different values call ``get_settings.cache_clear()`` or build a
    ``Settings`` instance directly.
    """
    return Settings()
