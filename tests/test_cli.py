import pytest

from talk_is_cheap import __version__
from talk_is_cheap.cli import build_parser, main


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args([])
    assert exit_info.value.code == 2


def test_db_check_reports_unreachable_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from talk_is_cheap.config import get_settings

    # Port 1 is never a Postgres server, so the connection fails fast.
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/none")
    get_settings.cache_clear()
    try:
        assert main(["db-check"]) == 1
    finally:
        get_settings.cache_clear()
    assert "unreachable" in capsys.readouterr().err


def test_ingest_requires_an_edgar_identity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from talk_is_cheap.config import get_settings

    monkeypatch.delenv("EDGAR_IDENTITY", raising=False)
    get_settings.cache_clear()
    try:
        assert main(["ingest", "edgar", "--tickers", "AAPL"]) == 1
    finally:
        get_settings.cache_clear()
    assert "EDGAR_IDENTITY is not set" in capsys.readouterr().err


def test_ingest_rejects_tickers_outside_the_universe(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ingest", "edgar", "--tickers", "ZZZZ"]) == 1
    assert "ZZZZ" in capsys.readouterr().err


def test_cli_startup_does_not_import_edgartools() -> None:
    """edgartools takes seconds to import; only `ingest` should pay for it."""
    import subprocess
    import sys

    code = "import sys, talk_is_cheap.cli; print('edgar' in sys.modules, 'pandas' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False False"
