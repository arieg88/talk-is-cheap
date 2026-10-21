"""API keys and rate limits.

Clients send a key in `X-API-Key`; it is compared with every key in API_KEYS in constant
time, and only its id (a hash prefix) is ever logged or stored. Without configured keys
every request is let in as `anonymous` (only in `local` and `test`: the app refuses to
start in production without keys).

Rate limits count requests per key over the last minute (a sliding window), chat
questions and everything else separately, in this process's memory.
"""

import hashlib
import hmac
import math
import time
from collections import deque
from collections.abc import Callable, Sequence

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader
from pydantic import SecretStr

ANONYMOUS = "anonymous"
WINDOW_SECONDS = 60.0

api_key_header = APIKeyHeader(
    name="X-API-Key", auto_error=False, description="one of the keys in API_KEYS"
)


def key_id(key: str) -> str:
    """What identifies a key in logs and the database, never the key itself."""
    return "key_" + hashlib.sha256(key.encode()).hexdigest()[:12]


class Keys:
    def __init__(self, keys: Sequence[SecretStr]) -> None:
        self._keys = [k.get_secret_value().encode() for k in keys]

    @property
    def required(self) -> bool:
        return bool(self._keys)

    def identify(self, presented: str | None) -> str | None:
        """The id of a valid key, `anonymous` when no keys are configured, else None."""
        if not self._keys:
            return ANONYMOUS
        if not presented:
            return None
        candidate = presented.encode()
        # Compare with every key, so the time taken says nothing about which matched.
        matched = [hmac.compare_digest(candidate, k) for k in self._keys]
        return key_id(presented) if any(matched) else None


class RateLimiter:
    """At most `per_minute` requests per key in any 60 seconds."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute = per_minute
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> float | None:
        """Count a request; None when allowed, else the seconds until one would be."""
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= self.per_minute:
            return hits[0] + WINDOW_SECONDS - now
        hits.append(now)
        return None


def authenticate(request: Request, presented: str | None = Security(api_key_header)) -> str:
    """The caller's key id, or 401."""
    keys: Keys = request.app.state.keys
    found = keys.identify(presented)
    if found is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing or invalid API key: send one of API_KEYS in X-API-Key",
            headers={"WWW-Authenticate": "APIKey"},
        )
    request.state.key_id = found
    return found


def limit(kind: str) -> Callable[[Request, str], None]:
    """A dependency counting the request against the `kind` limit ("chat" or "default")
    of its key; 429 with Retry-After when over it."""

    def check(request: Request, key: str = Security(authenticate)) -> None:
        limiter: RateLimiter = request.app.state.limiters[kind]
        wait = limiter.hit(key)
        if wait is not None:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"rate limit: {limiter.per_minute} {kind} requests a minute per key",
                headers={"Retry-After": str(max(1, math.ceil(wait)))},
            )

    return check
