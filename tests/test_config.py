from __future__ import annotations

from pathlib import Path

import pytest

from tell import __version__
from tell.config import Settings, get_settings

EXPECTED_ENV_VARS = {
    "DATABASE_URL",
    "SEC_USER_AGENT",
    "TELL_HTTP_USER_AGENT",
    "SLACK_WEBHOOK_URL",
    "GITHUB_TOKEN",
    "TELL_DATA_REPO",
    "TELL_DATA_PUSH_TOKEN",
    "TELL_POLL_CONCURRENCY",
    "TELL_RATE_EDGAR_RPS",
    "TELL_RATE_NEWS_RPS",
    "TELL_RATE_ATS_RPS",
    "TELL_DISCOVERY_BUDGET",
    "TELL_DISCOVERY_RPS",
    "TELL_BASE_URL",
    "TELL_API_KEY",
    "TELL_ALERT_THRESHOLD",
    "TELL_SLACK_TZ",
    "TELL_SLACK_DIGEST_HOUR",
    "TELL_REVIEW_PASSWORD",
    "TELL_HTTP_TIMEOUT_S",
    "TELL_ENV",
}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in EXPECTED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env in cwd
    get_settings.cache_clear()


@pytest.mark.usefixtures("clean_env")
def test_defaults() -> None:
    s = Settings()
    assert s.database_url == "postgresql+psycopg://karankapur@localhost:5432/tell_dev"
    assert s.sec_user_agent == "tell/0.1 (contact@example.invalid)"
    assert s.tell_http_user_agent == "tell/0.1 (+https://github.com/karankapur/tell)"
    assert s.slack_webhook_url is None
    assert s.github_token is None
    assert s.tell_data_repo == "karankapur/tell-data"
    assert s.tell_data_push_token is None
    assert s.tell_poll_concurrency == 8
    assert s.tell_rate_edgar_rps == 5.0
    assert s.tell_rate_news_rps == 1.0
    assert s.tell_rate_ats_rps == 8.0
    assert s.tell_discovery_budget == 300
    assert s.tell_discovery_rps == 4.0
    assert s.tell_base_url == "https://karankapur.github.io/tell-data"
    assert s.tell_api_key is None
    assert s.tell_alert_threshold == 6.0
    assert s.tell_slack_tz == "America/Los_Angeles"
    assert s.tell_slack_digest_hour == 8
    assert s.tell_review_password is None
    assert s.tell_http_timeout_s == 20.0
    assert s.tell_env == "dev"


@pytest.mark.usefixtures("clean_env")
def test_env_overrides_and_secret_masking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELL_POLL_CONCURRENCY", "3")
    monkeypatch.setenv("TELL_RATE_EDGAR_RPS", "2.5")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.invalid/abc")
    monkeypatch.setenv("TELL_ENV", "ci")
    s = Settings()
    assert s.tell_poll_concurrency == 3
    assert s.tell_rate_edgar_rps == 2.5
    assert s.tell_env == "ci"
    assert s.slack_webhook_url is not None
    assert "hooks.slack" not in repr(s.slack_webhook_url)
    assert s.slack_webhook_url.get_secret_value() == "https://hooks.slack.invalid/abc"


@pytest.mark.usefixtures("clean_env")
def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


@pytest.mark.usefixtures("clean_env")
def test_env_file_is_read(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TELL_SLACK_DIGEST_HOUR=17\n")
    assert Settings().tell_slack_digest_hour == 17


def test_env_example_lists_every_variable_with_a_comment_and_no_values() -> None:
    root = Path(__file__).resolve().parents[1]
    lines = (root / ".env.example").read_text().splitlines()
    declared: dict[str, str] = {}
    previous = ""
    for line in lines:
        if not line.strip():
            continue
        if line.startswith("#"):
            previous = line
            continue
        name, _, value = line.partition("=")
        assert value == "", f"{name} must not carry a value in .env.example"
        assert previous.startswith("#"), f"{name} needs a one-line comment above it"
        declared[name] = previous
        previous = ""
    assert set(declared) == EXPECTED_ENV_VARS
    assert set(declared) == {name.upper() for name in Settings.model_fields}


def test_version_string() -> None:
    assert __version__ == "0.1.0"
