import pytest
from pydantic import ValidationError

from talk_is_cheap.config import Environment, Settings


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real environment and .env file out of these tests."""
    for name in ("ENVIRONMENT", "LOG_LEVEL", "DATABASE_URL", "DATABASE_ECHO", "DATABASE_POOL_SIZE"):
        monkeypatch.delenv(name, raising=False)


def make_settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_defaults_match_local_docker_database() -> None:
    settings = make_settings()
    assert settings.environment is Environment.LOCAL
    assert settings.log_level == "INFO"
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.database_url.endswith("@localhost:5432/talkischeap")
    assert settings.database_pool_size == 5


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db.internal:5432/app")
    monkeypatch.setenv("DATABASE_POOL_SIZE", "12")
    settings = make_settings()
    assert settings.environment is Environment.PRODUCTION
    assert settings.database_url == "postgresql+psycopg://u:p@db.internal:5432/app"
    assert settings.database_pool_size == 12


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@localhost/db",
        "postgresql+asyncpg://u:p@localhost/db",
        "sqlite:///local.db",
    ],
)
def test_rejects_database_url_without_psycopg_driver(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(ValidationError, match="postgresql\\+psycopg://"):
        make_settings()


@pytest.mark.parametrize("size", ["0", "51"])
def test_rejects_pool_size_out_of_range(monkeypatch: pytest.MonkeyPatch, size: str) -> None:
    monkeypatch.setenv("DATABASE_POOL_SIZE", size)
    with pytest.raises(ValidationError):
        make_settings()


def test_rejects_unknown_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "VERBOSE")
    with pytest.raises(ValidationError):
        make_settings()
