"""Polite, cached HTTP for archive and news API sources (`ingest archives`, `ingest
finnhub`).

Every final response (2xx, 404) is kept on disk under its request (URL, parameters and
Range header), so nothing is fetched twice: a re-run, or a second search that asks the
same question, reads the file. Requests to one service start at least `interval` seconds
apart. Overload answers (429, 5xx) and dropped connections are retried after a pause
that doubles each time (`BACKOFF_START` to `BACKOFF_MAX`); after `max_attempts` the
request fails with `ServiceUnavailable` and the caller leaves the work for the next run.

A service can instead be given a `RetryPolicy`, waits matched to each kind of failure
(the Wayback Machine's): server errors retried at once and then after a few seconds, a
504 not retried at all (`GatewayTimeout`: the caller asks a smaller question), a 429
retried once after Retry-After, and connection timeouts retried at the normal pace.
Several timeouts in a row are taken for a block: with a `probe` request configured, the
fetcher pauses, sending only the probe once a minute, and resumes at the normal pace as
soon as a probe is answered; other fetchers and other work go on meanwhile. Every wait
and pause is reported to `on_wait` with its reason and length.
"""

import asyncio
import gzip
import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

BACKOFF_START = 5.0  # seconds
BACKOFF_MAX = 120.0
MAX_ATTEMPTS = 8
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
# Statuses that are an answer, kept in the cache; anything else is not cached.
CACHED_STATUS = frozenset({200, 206, 301, 302, 303, 307, 308, 404})
TIMEOUT = httpx.Timeout(120.0, connect=30.0)


class ServiceUnavailable(Exception):
    """A service kept refusing or dropping one request."""


class GatewayTimeout(ServiceUnavailable):
    """504 under a `RetryPolicy`: the question is too large to answer in time. Asking it
    again would not help; a smaller one may."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How long to wait after each kind of failure before asking again."""

    # 500, 502, 503 and answers `accept` refuses: one retry after each wait, then give up.
    server_error_waits: tuple[float, ...] = (0.0, 2.0, 4.0, 8.0)
    # 429: retried once, after Retry-After, or this long when it is missing.
    rate_limit_wait: float = 60.0
    # Connection timeouts: retried at the normal pace, as many times as server errors.
    # This many in a row (across requests) is a block: with a probe, the fetcher sends
    # only the probe, every `probe_every` seconds, until one is answered.
    block_after: int = 3
    probe_every: float = 60.0


@dataclass(frozen=True, slots=True)
class CachedResponse:
    url: str  # the request URL with its parameters
    status: int
    body: bytes
    headers: Mapping[str, str]
    cached: bool = False

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass(frozen=True, slots=True)
class Attempt:
    """One request that was sent (cache hits are not)."""

    service: str
    url: str
    number: int
    outcome: str  # "HTTP 200", "HTTP 504", "ReadTimeout"
    seconds: float


class PoliteFetcher:
    """GET requests to one service: paced, retried with backoff, cached on disk."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        cache_dir: Path,
        service: str,
        *,
        interval: float,
        max_attempts: int = MAX_ATTEMPTS,
        on_attempt: Callable[[Attempt], None] | None = None,
        policy: RetryPolicy | None = None,
        on_wait: Callable[[str, str], None] | None = None,
        probe: tuple[str, Mapping[str, str]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.cache_dir = cache_dir / service
        self.service = service
        self.interval = interval
        self.max_attempts = max_attempts
        self.on_attempt = on_attempt
        self.policy = policy
        self.on_wait = on_wait
        # A small request (URL and parameters) that tells whether a block is over.
        self.probe = probe
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
        self._resume_at: float | None = None  # no request before this (a policy's wait)
        self._timeouts = 0  # connection timeouts in a row, across requests
        # Set while the fetcher is paused for a block, and while it is not, so other
        # work can start when a pause begins and know when it ends.
        self.blocked = asyncio.Event()
        self.unblocked = asyncio.Event()
        self.unblocked.set()
        self._lock = asyncio.Lock()
        self.requests = 0
        self.cache_hits = 0

    def cached(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> CachedResponse | None:
        """The stored answer to this request, if any."""
        key = _key(url, params, headers)
        meta_path, body_path = self._paths(key)
        if not meta_path.exists() or not body_path.exists():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        body = gzip.decompress(body_path.read_bytes())
        return CachedResponse(meta["url"], meta["status"], body, meta["headers"], cached=True)

    async def get(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        *,
        cache: bool = True,
        redact: Callable[[str], str] = lambda u: u,
        accept: Callable[[CachedResponse], bool] = lambda r: True,
        follow_redirects: bool = True,
    ) -> CachedResponse:
        """The answer to a GET, from the cache when it holds one. `cache=False` fetches
        and stores nothing (an answer that is still changing). `redact` hides secrets
        (an API key) in what is stored and reported. An answer `accept` refuses (a
        maintenance page sent with status 200) is retried like an overload, and never
        cached. With `follow_redirects=False` a redirect is the answer (its Location
        header is kept)."""
        if cache:
            found = self.cached(url, params, headers)
            if found is not None:
                self.cache_hits += 1
                return found
        pause = BACKOFF_START
        outcome = ""
        failures: Counter[str] = Counter()  # this request's failures, by kind
        attempt = 0
        while True:
            attempt += 1
            error: httpx.TransportError | None = None
            async with self._lock:
                await self._wait()
                sent = self._clock()
                self.requests += 1
                try:
                    response = await self.client.get(
                        url,
                        params=params,
                        headers=dict(headers or {}),
                        timeout=TIMEOUT,
                        follow_redirects=follow_redirects,
                    )
                except httpx.TransportError as exc:
                    outcome = f"{type(exc).__name__}: {exc}".rstrip(": ")[:200]
                    response = None
                    error = exc
                finally:
                    self._last = self._clock()
            answer = None
            if response is not None:
                outcome = f"HTTP {response.status_code}"
                if response.status_code not in RETRY_STATUS:
                    answer = CachedResponse(
                        redact(str(response.url)),
                        response.status_code,
                        response.content,
                        {k.lower(): v for k, v in response.headers.items()},
                    )
                    if not accept(answer):
                        outcome += f", unexpected answer: {answer.text[:80]!r}"
                        answer = None
            shown = redact(_full_url(url, params))
            self._report(shown, attempt, outcome, sent)
            if isinstance(error, httpx.ConnectTimeout):
                self._timeouts += 1
            else:
                self._timeouts = 0
            if answer is not None:
                if cache and answer.status in CACHED_STATUS:
                    self._store(_key(url, params, headers), answer)
                return answer
            if self.policy is not None:
                if self.probe is not None and self._timeouts >= self.policy.block_after:
                    await self._pause_for_block(self.policy, self.probe, shown)
                    continue  # the same request again, at the normal pace
                decided = self._decide(self.policy, response, outcome, failures, shown)
                if decided is None:
                    break
                wait, reason = decided
                self._resume_at = self._clock() + wait
                self._notify(reason, shown)
                continue
            if attempt >= self.max_attempts:
                break
            retry_after = _retry_after(response)
            await self._sleep(max(pause, retry_after))
            pause = min(pause * 2, BACKOFF_MAX)
        raise ServiceUnavailable(
            f"{self.service} kept failing: {attempt} attempts for "
            f"{redact(_full_url(url, params))[:200]} (last: {outcome})"
        )

    def _decide(
        self,
        policy: RetryPolicy,
        response: httpx.Response | None,
        outcome: str,
        failures: Counter[str],
        url: str,
    ) -> tuple[float, str] | None:
        """How long to wait before asking again, and why; None to give up."""
        status = response.status_code if response is not None else None
        if status == 504:
            raise GatewayTimeout(f"{self.service}: HTTP 504 for {url[:200]}")
        if status == 429:
            if failures["rate limit"]:
                return None
            failures["rate limit"] += 1
            after = _retry_after(response)
            if after:
                return after, f"HTTP 429: waiting {after:g}s as Retry-After asks, then one retry"
            wait = policy.rate_limit_wait
            return wait, f"HTTP 429 without Retry-After: waiting {wait:g}s, then one retry"
        waits = policy.server_error_waits
        if response is not None:  # 500, 502, 503, or an answer `accept` refused
            done = failures["server error"]
            if done >= len(waits):
                return None
            failures["server error"] += 1
            when = "at once" if not waits[done] else f"in {waits[done]:g}s"
            return waits[done], f"{outcome}: retry {done + 1} of {len(waits)} {when}"
        done = failures["connection"]
        if done >= len(waits):
            return None
        failures["connection"] += 1
        return 0.0, f"{outcome}: retry {done + 1} of {len(waits)} at the normal pace"

    async def _pause_for_block(
        self, policy: RetryPolicy, probe: tuple[str, Mapping[str, str]], url: str
    ) -> None:
        """Send nothing but the probe, every `probe_every` seconds, until one is answered
        (HTTP 200). The fetcher's lock is held, so its other requests wait too."""
        probe_url, probe_params = probe
        shown = _full_url(probe_url, probe_params)
        started = self._clock()
        self._notify(
            f"{self._timeouts} connection timeouts in a row: taken for a block; paused, "
            f"one probe every {policy.probe_every:g}s until one is answered",
            url,
        )
        probes = 0
        self.unblocked.clear()
        self.blocked.set()
        async with self._lock:
            while True:
                await self._sleep(policy.probe_every)
                probes += 1
                self.requests += 1
                sent = self._clock()
                try:
                    response = await self.client.get(
                        probe_url, params=dict(probe_params), timeout=TIMEOUT
                    )
                    outcome = f"HTTP {response.status_code}"
                    answered = response.status_code == 200
                except httpx.TransportError as exc:
                    outcome, answered = type(exc).__name__, False
                finally:
                    self._last = self._clock()
                self._report(shown, probes, f"probe: {outcome}", sent)
                if answered:
                    break
        self.blocked.clear()
        self.unblocked.set()
        self._timeouts = 0
        length = self._clock() - started
        self._notify(
            f"pause over after {_duration(length)} ({probes} probe{'s' * (probes != 1)}): "
            "resuming at the normal pace",
            url,
        )

    def _notify(self, reason: str, url: str) -> None:
        if self.on_wait is not None:
            self.on_wait(reason, url)

    async def _wait(self) -> None:
        start = self._last + self.interval if self._last is not None else None
        if self._resume_at is not None:
            start = self._resume_at if start is None else max(start, self._resume_at)
        if start is None:
            return
        delay = start - self._clock()
        if delay > 0:
            await self._sleep(delay)

    def _report(self, url: str, attempt: int, outcome: str, sent: float) -> None:
        if self.on_attempt is not None:
            self.on_attempt(Attempt(self.service, url, attempt, outcome, self._clock() - sent))

    def _paths(self, key: str) -> tuple[Path, Path]:
        folder = self.cache_dir / key[:2]
        return folder / f"{key}.json", folder / f"{key}.body.gz"

    def _store(self, key: str, response: CachedResponse) -> None:
        meta_path, body_path = self._paths(key)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(gzip.compress(response.body))
        keep = ("content-type", "content-range", "location", "x-ratelimit-remaining")
        meta = {
            "url": response.url,
            "status": response.status,
            "headers": {k: v for k, v in response.headers.items() if k in keep},
        }
        meta_path.write_text(json.dumps(meta), encoding="utf-8")


def _key(url: str, params: Mapping[str, str] | None, headers: Mapping[str, str] | None) -> str:
    request = {
        "url": url,
        "params": sorted((params or {}).items()),
        "range": (headers or {}).get("Range"),
    }
    return hashlib.sha256(json.dumps(request).encode()).hexdigest()


def _full_url(url: str, params: Mapping[str, str] | None) -> str:
    return str(httpx.URL(url, params=dict(params))) if params else url


def _duration(seconds: float) -> str:
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes} min {rest} s" if minutes else f"{rest} s"


def _retry_after(response: httpx.Response | None) -> float:
    if response is None:
        return 0.0
    value = response.headers.get("retry-after", "")
    try:
        return min(float(value), BACKOFF_MAX)
    except ValueError:
        return 0.0
