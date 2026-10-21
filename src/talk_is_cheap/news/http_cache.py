"""Polite, cached HTTP for archive and news API sources (`ingest archives`, `ingest
finnhub`).

Every final response (2xx, 404) is kept on disk under its request (URL, parameters and
Range header), so nothing is fetched twice: a re-run, or a second search that asks the
same question, reads the file. Requests to one service start at least `interval` seconds
apart. Overload answers (429, 5xx) and dropped connections are retried after a pause
that doubles each time (`BACKOFF_START` to `BACKOFF_MAX`); after `max_attempts` the
request fails with `ServiceUnavailable` and the caller leaves the work for the next run.
"""

import asyncio
import gzip
import hashlib
import json
import time
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
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.cache_dir = cache_dir / service
        self.service = service
        self.interval = interval
        self.max_attempts = max_attempts
        self.on_attempt = on_attempt
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
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
        for attempt in range(1, self.max_attempts + 1):
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
            self._report(redact(_full_url(url, params)), attempt, outcome, sent)
            if answer is not None:
                if cache and answer.status in CACHED_STATUS:
                    self._store(_key(url, params, headers), answer)
                return answer
            if attempt < self.max_attempts:
                retry_after = _retry_after(response)
                await self._sleep(max(pause, retry_after))
                pause = min(pause * 2, BACKOFF_MAX)
        raise ServiceUnavailable(
            f"{self.service} kept failing: {self.max_attempts} attempts for "
            f"{redact(_full_url(url, params))[:200]} (last: {outcome})"
        )

    async def _wait(self) -> None:
        if self._last is None:
            return
        delay = self._last + self.interval - self._clock()
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


def _retry_after(response: httpx.Response | None) -> float:
    if response is None:
        return 0.0
    value = response.headers.get("retry-after", "")
    try:
        return min(float(value), BACKOFF_MAX)
    except ValueError:
        return 0.0
