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


@pytest.mark.parametrize("source", ["edgar", "filings"])
def test_ingest_requires_an_edgar_identity(
    source: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from talk_is_cheap.config import get_settings

    # An empty variable overrides a local .env file and counts as unset.
    monkeypatch.setenv("EDGAR_IDENTITY", "")
    get_settings.cache_clear()
    try:
        assert main(["ingest", source, "--tickers", "AAPL"]) == 1
    finally:
        get_settings.cache_clear()
    assert "EDGAR_IDENTITY is not set" in capsys.readouterr().err


def test_show_rejects_malformed_periods(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["show", "NVDA", "2026-Q3", "outlook"]) == 1
    assert "FY2026Q3" in capsys.readouterr().err


def test_show_accepts_only_known_section_types() -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["show", "NVDA", "latest", "guidance"])
    assert exit_info.value.code == 2


@pytest.mark.parametrize("source", ["edgar", "filings", "news"])
def test_ingest_rejects_tickers_outside_the_universe(
    source: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["ingest", source, "--tickers", "ZZZZ"]) == 1
    assert "ZZZZ" in capsys.readouterr().err


def test_cli_startup_does_not_import_edgartools() -> None:
    """edgartools takes seconds to import; only `ingest` should pay for it. The same goes
    for the HTTP, feed parsing and fuzzy matching libraries news uses."""
    import subprocess
    import sys

    heavy = ("edgar", "pandas", "httpx", "feedparser", "rapidfuzz")
    code = f"import sys, talk_is_cheap.cli; print([m for m in {heavy!r} if m in sys.modules])"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"


# Commands that print more than `head -1` reads. "late" writes its last line after `head`
# has exited, so the closed pipe only shows when Python flushes stdout at exit.
_PRINTERS = {
    "flood": "for n in range(200_000):\n        print(f'line {n}')",
    "late": ("print('line 0', flush=True)\n    time.sleep(1)\n    print('line 1')"),
}


@pytest.mark.parametrize("printer", sorted(_PRINTERS))
def test_output_piped_into_head_exits_quietly(printer: str) -> None:
    """`talk-is-cheap show ... | head` stops reading early; that is not an error to report."""
    import shlex
    import subprocess
    import sys

    code = (
        "import sys, time\n"
        "import talk_is_cheap.cli as cli\n"
        "def run(args):\n"
        f"    {_PRINTERS[printer]}\n"
        "    return 0\n"
        "cli._run = run\n"
        "sys.exit(cli.main(['companies']))\n"
    )
    result = subprocess.run(
        f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} | head -1",
        shell=True,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert result.stdout == "line 0\n"
    assert result.stderr == ""


def test_backfill_needs_a_start_date(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ingest", "news", "--backfill"]) == 2
    assert "--since" in capsys.readouterr().err


def test_poll_takes_no_dates(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ingest", "news", "--since", "2024-01-01"]) == 2
    assert "--backfill" in capsys.readouterr().err


def test_news_rejects_malformed_quarters(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["news", "NVDA", "--around-earnings", "2027-Q2"]) == 1
    assert "FY2026Q3" in capsys.readouterr().err


def test_reprocess_takes_no_other_options(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ingest", "news", "--reprocess", "--tickers", "NVDA"]) == 2
    assert "--reprocess" in capsys.readouterr().err


def test_an_interrupted_backfill_says_how_to_resume(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ctrl+C (or `kill`, which the backfill treats the same) stops with a summary."""
    import talk_is_cheap.cli as cli

    def interrupted(coroutine: object) -> int:
        coroutine.close()  # type: ignore[attr-defined]
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.asyncio, "run", interrupted)
    assert main(["ingest", "news", "--backfill", "--since", "2024-01-01"]) == 130
    out = capsys.readouterr().out
    assert "stopped: no window finished in this run" in out
    assert "resumes where it stopped" in out


def test_backfill_summary_reports_the_average_window() -> None:
    from datetime import timedelta

    from talk_is_cheap.cli import _backfill_summary
    from talk_is_cheap.news.gdelt import BackfillProgress

    seen = [
        BackfillProgress("NVDA", 2, 146, 80, 9, None, fetched=2, elapsed=timedelta(minutes=6)),
        BackfillProgress("NVDA", 3, 146, 120, 13, None, fetched=3, elapsed=timedelta(minutes=9)),
    ]
    assert _backfill_summary(seen) == (
        "3 windows fetched in 9m00s (average 3m00s per window), 120 new articles, 1 company started"
    )


def test_backfill_takes_weeks_or_earnings_windows_not_both(
    capsys: pytest.CaptureFixture[str],
) -> None:
    both = ["ingest", "news", "--backfill", "--since", "2024-01-01", "--around-earnings"]
    assert main(both) == 2
    assert "--around-earnings" in capsys.readouterr().err
    negative = ["ingest", "news", "--backfill", "--around-earnings", "--days-after", "-1"]
    assert main(negative) == 2
