"""`talk-is-cheap ingest archives` and `ingest finnhub`."""

import asyncio
import contextlib
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from talk_is_cheap import __version__
from talk_is_cheap.config import Settings, get_settings
from talk_is_cheap.db import create_engine, create_session_factory
from talk_is_cheap.universe import Universe, load_universe

if TYPE_CHECKING:
    from talk_is_cheap.news.archives import MonthProgress

# Wayback searches cover the months that ended at least this long ago: the archive is
# still adding captures of later days, which RSS polling and Finnhub cover.
SETTLE_TIME = timedelta(days=7)


def _stamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def research_agent(settings: Settings, purpose: str) -> str | None:
    """The User-Agent of archive and API requests, with the EDGAR_IDENTITY contact."""
    if not settings.edgar_identity:
        return None
    return f"talk-is-cheap/{__version__} ({purpose}; contact: {settings.edgar_identity})"


def _universe() -> Universe | None:
    settings = get_settings()
    try:
        return load_universe(settings.universe_file)
    except (OSError, ValueError) as exc:
        print(f"cannot use {settings.universe_file}: {exc}", file=sys.stderr)
        return None


async def archives_command(
    publishers: Sequence[str] | None,
    tickers: Sequence[str] | None,
    since: date | None,
    dry: bool,
    limit: int | None,
) -> int:
    from talk_is_cheap.news.archive_index import (
        CC_INTERVAL,
        COMMON_CRAWL,
        WAYBACK,
        WAYBACK_INTERVAL,
        WAYBACK_PAGE_INTERVAL,
        WAYBACK_PROBE,
        WAYBACK_RETRY,
        ArchiveError,
        crawls,
    )
    from talk_is_cheap.news.archives import (
        ArchivePublisher,
        Download,
        DownloadSummary,
        MonthProgress,
        SearchResult,
        SearchUnit,
        SlugFilter,
        capture_counts,
        cc_units,
        covered_months,
        crawl_holds,
        download,
        load_archives_file,
        month_end,
        month_progress,
        months,
        pending,
        record_empty,
        run_search,
        search_states,
        searched,
        wayback_units,
    )
    from talk_is_cheap.news.article_text import HostRules
    from talk_is_cheap.news.http_cache import Attempt, PoliteFetcher, ServiceUnavailable
    from talk_is_cheap.news.linking import CompanyMatcher
    from talk_is_cheap.news.locks import ARCHIVES_LOCK, advisory_locks
    from talk_is_cheap.news.poll import new_client

    settings = get_settings()
    universe = _universe()
    if universe is None:
        return 1
    try:
        config = load_archives_file(settings.archives_file)
        selected = config.select(publishers)
        companies = universe.select_news(tickers) if tickers else universe.select_news()
    except (OSError, ValueError) as exc:
        print(f"cannot use {settings.archives_file}: {exc}", file=sys.stderr)
        return 1
    agent = research_agent(settings, "research: reads archived news for executives' statements")
    if agent is None:
        print("EDGAR_IDENTITY must be set: archive requests carry it as the contact")
        return 1
    start = since or universe.history_start
    today = datetime.now(UTC).date()
    wanted = [c.ticker for c in companies] if tickers else None
    # Every company's names are searched (a search is recorded for all of them);
    # --tickers only narrows what is counted and downloaded.
    slugs = SlugFilter([c for c in universe.companies if c.name], config.claim_words)

    def attempt(a: Attempt) -> None:
        if a.outcome not in ("HTTP 200", "HTTP 206", "HTTP 404"):
            print(
                f"{_stamp()}   {a.service} attempt {a.number}: {a.outcome} in "
                f"{a.seconds:.1f}s  {a.url[:110]}",
                flush=True,
            )

    def searched_line(r: SearchResult) -> None:
        unit = r.unit
        if r.error is not None:
            print(
                f"{_stamp()} {unit.publisher.key:<15} {unit.source:<11} {unit.period:<17} "
                f"{unit.prefix[:60]:<60} FAILED: {r.error[:160]}",
                file=sys.stderr,
                flush=True,
            )
            return
        print(
            f"{_stamp()} {unit.publisher.key:<15} {unit.source:<11} {unit.period:<17} "
            f"{unit.prefix[:60]:<60} {r.captures:>5} captures, {r.articles:>4} articles, "
            f"{r.kept:>3} kept, {r.already_stored} stored before ({r.requests} requests)"
            + (f", {r.truncated_pages} pages cut off by the index" if r.truncated_pages else ""),
            flush=True,
        )

    engine = create_engine(settings)
    try:
        async with advisory_locks(engine, ARCHIVES_LOCK) as locked:
            if not locked:
                print("another `ingest archives` run is in progress", file=sys.stderr)
                return 1
            sessions = create_session_factory(engine)
            async with new_client(agent) as client, contextlib.AsyncExitStack() as stack:
                cache = settings.news_cache_dir
                cc = PoliteFetcher(
                    client, cache, COMMON_CRAWL, interval=CC_INTERVAL, on_attempt=attempt
                )

                def waited(reason: str, url: str) -> None:
                    print(f"{_stamp()}   wayback: {reason}  {url[:100]}", flush=True)

                # CDX searches, paused while the archive blocks us; archived pages are read
                # by a fetcher of their own, which never pauses, so downloads go on.
                wayback = PoliteFetcher(
                    client,
                    cache,
                    WAYBACK,
                    interval=WAYBACK_INTERVAL,
                    on_attempt=attempt,
                    policy=WAYBACK_RETRY,
                    on_wait=waited,
                    probe=WAYBACK_PROBE,
                )
                wayback_pages = PoliteFetcher(
                    client, cache, WAYBACK, interval=WAYBACK_PAGE_INTERVAL,
                    on_attempt=attempt, policy=WAYBACK_RETRY, on_wait=waited,
                )  # fmt: skip
                try:
                    found_crawls = await crawls(cc, start, today)
                except (ServiceUnavailable, ArchiveError) as exc:
                    print(f"Common Crawl's crawl list is unavailable: {exc}", file=sys.stderr)
                    return 1
                print(
                    f"{_stamp()} {len(found_crawls)} Common Crawl crawls since {start}: "
                    f"{found_crawls[0].id if found_crawls else '-'} to "
                    f"{found_crawls[-1].id if found_crawls else '-'}",
                    flush=True,
                )
                # Common Crawl and the Wayback Machine are searched at the same time: a
                # publisher's Wayback months are known once its Common Crawl pass is done.
                failures: list[str] = []
                # Index pages of each Wayback section, asked once a run.
                page_counts: dict[str, int] = {}
                # Each publisher's Wayback months after its searches.
                progress: dict[str, MonthProgress] = {}

                def page_line(unit: SearchUnit, done: int, pages: int, r: SearchResult) -> None:
                    if pages > 1:
                        print(
                            f"{_stamp()}   {unit.publisher.key} {unit.period} {unit.prefix[:50]}: "
                            f"page {done} of {pages}, {r.captures} captures, {r.kept} kept",
                            flush=True,
                        )

                def split_line(unit: SearchUnit, why: str) -> None:
                    print(
                        f"{_stamp()}   {unit.publisher.key} {unit.period} "
                        f"{unit.prefix[:50]}: {why}",
                        flush=True,
                    )

                async def run(fetcher: PoliteFetcher, units: list[SearchUnit]) -> None:
                    for unit in units:
                        result = await run_search(
                            sessions, fetcher, unit, slugs, start, on_page=page_line,
                            on_split=split_line, page_counts=page_counts,
                        )  # fmt: skip
                        if result.error is not None:
                            failures.append(unit.prefix)
                        searched_line(result)

                async def common_crawl(
                    group: list[ArchivePublisher], ready: "asyncio.Queue[ArchivePublisher | None]"
                ) -> None:
                    for publisher in group:
                        if not publisher.common_crawl:
                            print(
                                f"{_stamp()} {publisher.key}: Common Crawl search off "
                                "(archives.yaml); Wayback only",
                                flush=True,
                            )
                            await ready.put(publisher)
                            continue
                        async with sessions() as session:
                            done = await searched(session, publisher.key, COMMON_CRAWL)
                        units = [
                            u
                            for u in cc_units(publisher, found_crawls, start)
                            if (u.period, u.prefix) not in done
                        ]
                        print(
                            f"{_stamp()} {publisher.key}: {len(units)} Common Crawl searches "
                            f"to run ({len(done)} done before)",
                            flush=True,
                        )
                        by_crawl: dict[str, list[SearchUnit]] = {}
                        for unit in units:
                            by_crawl.setdefault(unit.period, []).append(unit)
                        for crawl_id, crawl_units in by_crawl.items():
                            if len(crawl_units) > 1 and not await crawl_holds(cc, crawl_units[0]):
                                for unit in crawl_units:
                                    await record_empty(sessions, unit)
                                print(
                                    f"{_stamp()} {publisher.key:<15} commoncrawl {crawl_id:<17} "
                                    f"holds nothing of {publisher.domain}: "
                                    f"{len(crawl_units)} searches recorded empty",
                                    flush=True,
                                )
                                continue
                            await run(cc, crawl_units)
                        await ready.put(publisher)
                    await ready.put(None)

                async def wayback_machine(
                    ready: "asyncio.Queue[ArchivePublisher | None]",
                ) -> None:
                    while (publisher := await ready.get()) is not None:
                        async with sessions() as session:
                            covered = await covered_months(session, publisher.key)
                            states = await search_states(session, publisher.key, WAYBACK)
                        every = [
                            m for m in months(start, today) if month_end(m) <= today - SETTLE_TIME
                        ]
                        missing = [m for m in every if m not in covered]
                        needed = wayback_units(publisher, missing, slugs.company_terms)
                        units = [u for u in needed if not states.get((u.period, u.prefix))]
                        before = month_progress(needed, states)
                        print(
                            f"{_stamp()} {publisher.key}: Common Crawl holds articles for "
                            f"{len(every) - len(missing)} of {len(every)} months; Wayback "
                            f"months: {_months(before)}; {len(units)} searches to run",
                            flush=True,
                        )
                        await run(wayback, units)
                        async with sessions() as session:
                            states = await search_states(session, publisher.key, WAYBACK)
                        progress[publisher.key] = month_progress(needed, states)

                def downloaded(d: Download) -> None:
                    size = f", {d.text_chars:,} chars from {d.text_source}" if d.text_chars else ""
                    why = f" ({d.reason})" if d.reason else ""
                    print(
                        f"{_stamp()}   {d.publisher:<15} {d.status}{why}{size}  "
                        f"{(d.url or '')[:100]}",
                        flush=True,
                    )

                # Two rounds: publishers marked `deferred` are searched and downloaded after
                # every other publisher's search and downloads.
                rounds = [
                    [p for p in selected if not p.deferred],
                    [p for p in selected if p.deferred],
                ]
                # Live pages are fetched as `ingest article-text` fetches them: its
                # User-Agent, robots.txt rules and pacing.
                live_client = await stack.enter_async_context(
                    new_client(settings.article_user_agent)
                )
                rules = HostRules(
                    live_client, settings.article_user_agent.split()[0].split("/")[0],
                    robots_dir=settings.news_cache_dir / "robots",
                )  # fmt: skip
                matcher = CompanyMatcher([c for c in universe.companies if c.name])
                summaries: list[DownloadSummary] = []
                archive_copies = {COMMON_CRAWL: cc, WAYBACK: wayback_pages}
                by_key = {p.key: p for p in config.publishers}

                async def download_pending(keys: list[str], most: int | None, when: str) -> int:
                    """Download the round's pending captures (at most `most`); how many."""
                    async with sessions() as session:
                        queue = await pending(session, keys, wanted, start, most)
                    if not queue and when:
                        return 0
                    print(
                        f"{_stamp()} downloading {len(queue)} articles{when} from their live "
                        "publisher pages (robots.txt, one request a second per domain, domains "
                        "in parallel); the archive copy only where the live page is gone",
                        flush=True,
                    )
                    summaries.append(
                        await download(
                            sessions,
                            rules,
                            archive_copies,
                            queue,
                            by_key,
                            matcher,
                            start,
                            on_result=downloaded,
                        )
                    )
                    return len(queue)

                async def while_paused(keys: list[str], searching: asyncio.Event) -> int:
                    """Download what the round has found whenever its Wayback searches are
                    paused for a block, until the searches end; how many were started."""
                    started = 0
                    while True:
                        if not await _first(wayback.blocked, searching):
                            return started
                        most = None if limit is None else limit - started
                        if most is None or most > 0:
                            started += await download_pending(
                                keys, most, " while Wayback searches are paused"
                            )
                        if not await _first(wayback.unblocked, searching):
                            return started

                for group in rounds:
                    if not group:
                        continue
                    keys = [p.key for p in group]
                    ready: asyncio.Queue[ArchivePublisher | None] = asyncio.Queue()
                    searching = asyncio.Event()  # set when the round's searches are over
                    early = None
                    if not dry:
                        early = asyncio.ensure_future(while_paused(keys, searching))
                    try:
                        await asyncio.gather(common_crawl(group, ready), wayback_machine(ready))
                    finally:
                        searching.set()
                    started = await early if early is not None else 0
                    _print_progress({p.key: progress[p.key] for p in group if p.key in progress})
                    async with sessions() as session:
                        counts = await capture_counts(session, keys, wanted, start)
                    _print_counts(counts.by_publisher, "PUBLISHER")
                    _print_counts(counts.by_company, "COMPANY")
                    print(
                        "captures by status: "
                        + ", ".join(f"{n} {s}" for s, n in counts.by_status.most_common())
                        + "\n",
                        flush=True,
                    )
                    if dry:
                        continue
                    rest = None if limit is None else max(limit - started, 0)
                    if rest != 0:
                        await download_pending(keys, rest, "")
                if failures:
                    failed = sum(m.failed for m in progress.values())
                    print(
                        f"{len(failures)} searches failed ({failed} Wayback months); the next "
                        "run searches only the failed and unsearched months",
                        file=sys.stderr,
                    )
                if dry:
                    return 0
    finally:
        await engine.dispose()
    print(
        f"\n{'PUBLISHER':<16}{'STORED':>8}{'UNAVAILABLE':>12}{'SKIPPED':>8}{'PENDING':>8}"
        f"{'TEXT LIVE':>10}{'WAYBACK':>9}{'CC':>5}"
    )
    for summary in summaries:
        for key, count in sorted(summary.by_publisher.items()):
            print(
                f"{key:<16}{count['stored']:>8}{count['unavailable']:>12}{count['skipped']:>8}"
                f"{count['pending']:>8}{count['text: the live page']:>10}"
                f"{count['text: the Wayback Machine']:>9}{count['text: Common Crawl']:>5}"
            )
    print(
        f"\n{sum(s.requests for s in summaries)} requests in "
        f"{sum(s.seconds for s in summaries):.0f}s"
    )
    return 0


async def _first(wanted: asyncio.Event, over: asyncio.Event) -> bool:
    """Wait for `wanted` or `over`; whether `wanted` came first (and `over` is not set)."""
    waits = [asyncio.ensure_future(e.wait()) for e in (wanted, over)]
    await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    for task in waits:
        task.cancel()
    return wanted.is_set() and not over.is_set()


def _months(found: "MonthProgress") -> str:
    return f"{found.done} done, {found.failed} failed, {found.left} left of {found.months}"


def _print_progress(rows: "dict[str, MonthProgress]") -> None:
    """Each publisher's Wayback months after the searches."""
    if not rows:
        return
    print(f"\n{'WAYBACK MONTHS':<16}{'DONE':>6}{'FAILED':>8}{'LEFT':>6}{'OF':>5}")
    for key, found in rows.items():
        print(f"{key:<16}{found.done:>6}{found.failed:>8}{found.left:>6}{found.months:>5}")


def _print_counts(rows: "dict[str, Counter[int]]", label: str) -> None:
    from talk_is_cheap.news.archives import years_of

    years = years_of(rows.values())
    print(f"\n{label:<16}" + "".join(f"{y:>7}" for y in years) + f"{'TOTAL':>8}")
    for key, counter in sorted(rows.items(), key=lambda kv: -sum(kv[1].values())):
        print(
            f"{key:<16}"
            + "".join(f"{counter[y]:>7}" for y in years)
            + f"{sum(counter.values()):>8}"
        )
    total: Counter[int] = Counter()
    for counter in rows.values():
        total.update(counter)
    print(f"{'TOTAL':<16}" + "".join(f"{total[y]:>7}" for y in years) + f"{sum(total.values()):>8}")


async def finnhub_command(tickers: Sequence[str] | None, since: date | None, dry: bool) -> int:
    from talk_is_cheap.news.finnhub import (
        MATCH_THRESHOLD,
        MIN_INTERVAL,
        SERVICE,
        SETTLE_TIME,
        FinnhubError,
        FinnhubItem,
        WindowResult,
        fetch_range,
        fetched_windows,
        finnhub_feed,
        google_stories,
        history_depth,
        match_stories,
        matched_ids,
        names_a_company,
        resolve,
        store_publisher_url,
        store_window,
        windows,
    )
    from talk_is_cheap.news.http_cache import Attempt, PoliteFetcher, ServiceUnavailable
    from talk_is_cheap.news.linking import CompanyMatcher
    from talk_is_cheap.news.locks import FINNHUB_LOCK, advisory_locks
    from talk_is_cheap.news.poll import new_client

    settings = get_settings()
    if settings.finnhub_api_key is None:
        print("FINNHUB_API_KEY is not set; Finnhub skipped")
        return 0
    key = settings.finnhub_api_key.get_secret_value()
    universe = _universe()
    if universe is None:
        return 1
    try:
        companies = universe.select_news(tickers)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    agent = research_agent(settings, "research: company news for executives' statements")
    if agent is None:
        print("EDGAR_IDENTITY must be set: Finnhub requests carry it as the contact")
        return 1
    today = datetime.now(UTC).date()
    matcher = CompanyMatcher([c for c in universe.companies if c.name])

    def attempt(a: Attempt) -> None:
        if a.outcome not in ("HTTP 200", "HTTP 302"):
            print(
                f"{_stamp()}   finnhub attempt {a.number}: {a.outcome} in {a.seconds:.1f}s",
                flush=True,
            )

    engine = create_engine(settings)
    try:
        async with advisory_locks(engine, FINNHUB_LOCK) as locked:
            if not locked:
                print("another `ingest finnhub` run is in progress", file=sys.stderr)
                return 1
            sessions = create_session_factory(engine)
            async with new_client(agent) as client:
                fetcher = PoliteFetcher(
                    client,
                    settings.news_cache_dir,
                    SERVICE,
                    interval=MIN_INTERVAL,
                    on_attempt=attempt,
                )
                try:
                    depth = await history_depth(fetcher, key, companies[0].ticker, today)
                except (FinnhubError, ServiceUnavailable) as exc:
                    print(f"Finnhub: {exc}", file=sys.stderr)
                    return 1
                print(
                    f"{_stamp()} Finnhub's free tier returns news back to {depth} "
                    f"({(today - depth).days} days)",
                    flush=True,
                )
                start = max(since or universe.history_start, depth)
                fetched: dict[str, list[tuple[WindowResult, bool]]] = {}
                items: list[FinnhubItem] = []
                for company in companies:
                    async with sessions() as session, session.begin():
                        feed = await finnhub_feed(session, company.ticker)
                        recorded = await fetched_windows(session, feed.id)
                    results = []
                    try:
                        for window_start, window_end in windows(start, today):
                            result = await fetch_range(
                                fetcher, key, company.ticker, window_start, window_end, today
                            )
                            results.append((result, window_start in recorded))
                    except (FinnhubError, ServiceUnavailable) as exc:
                        print(f"{_stamp()} {company.ticker:<6} FAILED: {exc}", file=sys.stderr)
                    fetched[company.ticker] = results
                    found = [i for r, _ in results for i in r.items]
                    items += found
                    print(
                        f"{_stamp()} {company.ticker:<6} {len(results)} windows, "
                        f"{sum(r.requests for r, _ in results)} requests, {len(found)} items, "
                        f"{sum(r.truncated for r, _ in results)} truncated",
                        flush=True,
                    )

                async with sessions() as session:
                    stories = await google_stories(session, [c.ticker for c in companies])
                matches = match_stories(stories, items)
                accepted = [m for m in matches if m.score >= MATCH_THRESHOLD]
                bands = Counter(min(int(m.score) // 10 * 10, 90) for m in matches)
                print(
                    f"\n{len(stories)} stories with only a Google News link; best Finnhub "
                    f"item within the date tolerance for {len(matches)}; by score: "
                    + ", ".join(f"{b}+: {bands[b]}" for b in sorted(bands, reverse=True))
                    + f"; {len(accepted)} at or above {MATCH_THRESHOLD:.0f}",
                    flush=True,
                )
                used = matched_ids(matches)
                new_items = {
                    t: [
                        (
                            r,
                            [
                                i
                                for i in r.items
                                if i.id not in used and names_a_company(matcher, i.headline)
                            ],
                        )
                        for r, done in results
                        if not done
                    ]
                    for t, results in fetched.items()
                }
                to_store = sum(len(i) for rs in new_items.values() for _, i in rs)
                if dry:
                    print(
                        f"would resolve {len(accepted)} publisher URLs and store up to "
                        f"{to_store} new items whose headline names a company (no change made)"
                    )
                    return 0

                resolved = 0
                for match in accepted:
                    try:
                        url = await resolve(fetcher, match.item)
                    except ServiceUnavailable as exc:
                        print(f"{_stamp()}   resolving {match.item.url}: {exc}", file=sys.stderr)
                        continue
                    if url is None:
                        continue
                    await store_publisher_url(sessions, match, url)
                    resolved += 1
                print(
                    f"{_stamp()} publisher URLs stored for {resolved} of {len(accepted)} "
                    f"matched stories ({resolved / len(stories):.0%} of {len(stories)})"
                    if stories
                    else f"{_stamp()} no Google News stories to match",
                    flush=True,
                )

                settled = today - SETTLE_TIME
                for ticker, pairs in new_items.items():
                    async with sessions() as session, session.begin():
                        feed = await finnhub_feed(session, ticker)
                    new = 0
                    for result, keep in pairs:
                        new += await store_window(
                            sessions, feed, result, keep, matcher, record=result.end < settled
                        )
                    print(
                        f"{_stamp()} {ticker:<6} {new} new articles from "
                        f"{sum(len(k) for _, k in pairs)} items whose headline names a company",
                        flush=True,
                    )
    finally:
        await engine.dispose()
    print(f"\n{fetcher.requests} requests ({fetcher.cache_hits} answers from the cache)")
    return 0
