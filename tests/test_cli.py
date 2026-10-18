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
