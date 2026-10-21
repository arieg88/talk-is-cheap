"""`talk-is-cheap serve`: the API under uvicorn."""

import sys

from fastapi import FastAPI

from talk_is_cheap.api.app import ApiConfigError, check_settings, create_app
from talk_is_cheap.config import get_settings


def app_from_env() -> FastAPI:
    """The app for the settings in the environment and .env (uvicorn's factory)."""
    return create_app(get_settings())


def serve(host: str, port: int, reload: bool) -> int:
    import uvicorn

    try:
        check_settings(get_settings())
    except ApiConfigError as exc:
        print(f"cannot start the API: {exc}", file=sys.stderr)
        return 2
    # Requests are logged by the app itself (`api/observability.py`).
    uvicorn.run(
        "talk_is_cheap.api.server:app_from_env",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        access_log=False,
        proxy_headers=True,
    )
    return 0
