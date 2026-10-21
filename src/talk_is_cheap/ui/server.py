"""`talk-is-cheap ui`: Streamlit serving `ui/app.py`.

Streamlit runs in place of this process, so signals (Ctrl+C, `docker stop`) reach it
directly. The theme and server options are in `.streamlit/config.toml`, read from the
working directory.
"""

import os
import sys
from pathlib import Path

APP = Path(__file__).with_name("app.py")


def streamlit_command(host: str, port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(APP),
        "--server.address",
        host,
        "--server.port",
        str(port),
    ]


def serve_ui(host: str, port: int) -> int:
    command = streamlit_command(host, port)
    os.execv(command[0], command)
