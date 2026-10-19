"""Settings for the whole test run: the developer's .env file is never read, and every
database connection goes to the test database.

Integration tests recreate that database at the start of each session
(`tests/integration/conftest.py`). The development database named by DATABASE_URL in .env
is never read or written.
"""

import os

import pytest

from talk_is_cheap.config import Settings, get_settings

# Same server as docker-compose.yml and CI, its own database. Dropped and recreated by
# every test session, so its name must end in "_test".
DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg://talkischeap:talkischeap@localhost:5432/talkischeap_test"
)


def configured_test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL") or DEFAULT_TEST_DATABASE_URL


def pytest_configure(config: pytest.Config) -> None:
    # Tests see the settings' defaults, as in CI: values in .env or the shell (an EDGAR
    # identity, a universe file, the development database) must not change results.
    # Tests that need a setting set it themselves.
    Settings.model_config["env_file"] = None
    for name in Settings.model_fields:
        os.environ.pop(name.upper(), None)
    os.environ["DATABASE_URL"] = configured_test_database_url()
    get_settings.cache_clear()
