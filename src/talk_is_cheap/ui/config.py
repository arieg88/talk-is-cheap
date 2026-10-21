"""The UI's settings: where the API is and the key it is called with. Both stay on the
Streamlit server; the browser only ever sees rendered pages."""

from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class UiSettings(BaseSettings):
    """`UI_API_URL` and `UI_API_KEY`, from the environment or `.env`."""

    model_config = SettingsConfigDict(
        env_prefix="UI_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    api_url: str = "http://127.0.0.1:8080"
    # One of the API's API_KEYS; unset when the API runs without keys (local, test).
    api_key: SecretStr | None = None

    @field_validator("api_key", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("api_url")
    @classmethod
    def _no_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


@lru_cache
def get_ui_settings() -> UiSettings:
    return UiSettings()
