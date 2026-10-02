"""Adaptive per-host rate limiting for curl_cffi-based scrapers.

The limiter is a token bucket per host whose refill rate adapts based on
observed server responses:

    - 429 → cooldown (honoring Retry-After when present and sane), rate halved.
    - 5xx → rate reduced mildly.
    - 2xx/3xx → rate adopted from RateLimit-* / X-RateLimit-* server hints.
                 Heuristic upward climb is OFF by default; enable via
                 config.allow_rate_climb = True or allow_rate_climb=True.

State persists to the shared SQLite store, so learning survives restarts.

Public surface: RateLimiter, HostState, get_limiter, reset_limiters.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from .imports_integration import get_config
from .info_manager import Timer, get_logger, get_store

__all__ = ["HostState", "RateLimiter", "get_limiter", "reset_limiters"]


def _host_of(url_or_host: str) -> str:
    if "://" in url_or_host:
        return urlparse(url_or_host).netloc.lower() or url_or_host
    return url_or_host.lower()


# --- Header parsing ----------------------------------------------------------

def _parse_retry_after(value: str, cap: float) -> Optional[float]:
    """Return seconds to wait, clamped to [0, cap]. None if unparseable."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, min(float(value), cap))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        delta = dt.timestamp() - time.time()
        return max(0.0, min(delta, cap))
    except Exception:
        return None


def _parse_ratelimit_headers(headers: dict) -> dict:
    """Extract hints from X-RateLimit-* and RateLimit-* families."""
    out: dict = {}
    h = {k.lower(): v for k, v in headers.items()}

    for suffix in ("limit", "remaining", "reset"):
        k = f"x-ratelimit-{suffix}"
        if k in h:
            try:
                out[f"x_{suffix}"] = float(h[k])
            except (TypeError, ValueError):
                pass

    if "ratelimit-limit" in h:
        parts = str(h["ratelimit-limit"]).split(";")
        try:
            out["ietf_limit"] = float(parts[0])
        except (TypeError, ValueError):
            pass
        for p in parts[1:]:
            p = p.strip()
            if p.startswith("w="):
                try:
                    out["ietf_window"] = float(p[2:])
                except (TypeError, ValueError):
                    pass

    if "ratelimit-remaining" in h:
        try:
            out["ietf_remaining"] = float(h["ratelimit-remaining"])
        except (TypeError, ValueError):
            pass

    return out


# --- State -------------------------------------------------------------------

@dataclass
class HostState:
    host: str
    rate: float
    burst: float
    tokens: float
    last_refill: float
    cooldown_until: float = 0.0
    consecutive_ok: int = 0
    consecutive_fail: int = 0
    total_requests: int = 0
    total_429s: int = 0
    total_5xx: int = 0
    last_status: int = 0

    def to_dict(self) -> dict:
        return {
            "host": self.host, "rate": self.rate, "burst": self.burst,
            "tokens": self.tokens, "last_refill": self.last_refill,
            "cooldown_until": self.cooldown_until,
            "consecutive_ok": self.consecutive_ok,
            "consecutive_fail": self.consecutive_fail,
            "total_requests": self.total_requests,
            "total_429s": self.total_429s,
            "total_5xx": self.total_5xx,
            "last_status": self.last_status,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HostState":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


# --- Limiter -----------------------------------------------------------------

class RateLimiter:
    """Adaptive per-host token-bucket rate limiter.

        async with RateLimiter() as limiter:
            await limiter.acquire(url)
            resp = await session.get(url)
            limiter.observe(url, resp.status_code, dict(resp.headers))
    """

    def __init__(self,
                 default_rate: Optional[float] = None,
                 default_burst: Optional[float] = None,
                 min_rate: Optional[float] = None,
                 max_rate: Optional[float] = None,
                 max_retry_after: Optional[float] = None,
                 honor_retry_after: Optional[bool] = None,
                 allow_rate_climb: Optional[bool] = None,
                 proxy_provider: Optional[Callable[[str], Optional[str]]] = None) -> None:
        cfg = get_config()
        self.default_rate = default_rate if default_rate is not None else cfg.default_rate
        self.default_burst = default_burst if default_burst is not None else cfg.default_burst
        self.min_rate = min_rate if min_rate is not None else cfg.min_rate
        self.max_rate = max_rate if max_rate is not None else cfg.max_rate
        self.max_retry_after = max_retry_after if max_retry_after is not None else cfg.max_retry_after
        self.honor_retry_after = honor_retry_after if honor_retry_after is not None else cfg.honor_retry_after
        self.allow_rate_climb = (
            allow_rate_climb if allow_rate_climb is not None
            else getattr(cfg, "allow_rate_climb", False)
        )
        self.proxy_provider = proxy_provider if proxy_provider is not None else cfg.proxy_provider

        self._store = get_store()
        self._lock = threading.RLock()
        self._states: dict[str, HostState] = {}
        self._log = get_logger("crawler_checks")
        self._load_all()

    # ---- state ----

    def _load_all(self) -> None:
        try:
            for host, d in self._store.all_host_states().items():
                try:
                    self._states[host] = HostState.from_dict(d)
                except Exception:
                    continue
        except Exception as e:  # noqa: BLE001
            self._log.warn("failed loading host state", error=str(e))

    def _persist(self, host: str) -> None:
        try:
            self._store.save_host_state(host, self._states[host].to_dict())
        except Exception as e:  # noqa: BLE001
            self._log.warn("failed persisting host state", host=host, error=str(e))

    def _get(self, host: str) -> HostState:
        st = self._states.get(host)
        if st is None:
            now = time.time()
            st = HostState(
                host=host, rate=self.default_rate, burst=self.default_burst,
                tokens=self.default_burst, last_refill=now,
            )
            self._states[host] = st
        return st

    def _refill(self, st: HostState, now: float) -> None:
        elapsed = max(0.0, now - st.last_refill)
        st.tokens = min(st.burst, st.tokens + elapsed * st.rate)
        st.last_refill = now

    def _wait_time(self, st: HostState, now: float) -> float:
        if now < st.cooldown_until:
            return st.cooldown_until - now
        self._refill(st, now)
        if st.tokens >= 1.0:
            return 0.0
        return (1.0 - st.tokens) / max(st.rate, 1e-9)

    def _consume(self, st: HostState, now: float) -> None:
        self._refill(st, now)
        st.tokens -= 1.0
        st.total_requests += 1

    # ---- acquire ----

    async def acquire(self, url_or_host: str) -> float:
        host = _host_of(url_or_host)
        waited = 0.0
        while True:
            with self._lock:
                st = self._get(host)
                now = time.time()
                wait = self._wait_time(st, now)
                if wait <= 0:
                    self._consume(st, now)
                    return waited
            chunk = min(wait, 1.0)
            await asyncio.sleep(chunk)
            waited += chunk

    def acquire_sync(self, url_or_host: str) -> float:
        host = _host_of(url_or_host)
        waited = 0.0
        while True:
            with self._lock:
                st = self._get(host)
                now = time.time()
                wait = self._wait_time(st, now)
                if wait <= 0:
                    self._consume(st, now)
                    return waited
            chunk = min(wait, 0.1)
            time.sleep(chunk)
            waited += chunk

    # ---- observe ----

    def observe(self, url_or_host: str, status: int,
                headers: Optional[dict] = None) -> None:
        host = _host_of(url_or_host)
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        with self._lock:
            st = self._get(host)
            st.last_status = status
            now = time.time()

            if status == 429:
                st.total_429s += 1
                st.consecutive_ok = 0
                st.consecutive_fail += 1
                retry_after: Optional[float] = None
                if self.honor_retry_after and "retry-after" in headers:
                    retry_after = _parse_retry_after(headers["retry-after"],
                                                     self.max_retry_after)
                if retry_after is None:
                    retry_after = min(2.0 ** st.consecutive_fail, self.max_retry_after)
                st.cooldown_until = max(st.cooldown_until, now + retry_after)
                st.rate = max(self.min_rate, st.rate * 0.5)

            elif 500 <= status < 600:
                st.total_5xx += 1
                st.consecutive_ok = 0
                st.consecutive_fail += 1
                st.rate = max(self.min_rate, st.rate * 0.75)

            elif 200 <= status < 400:
                st.consecutive_ok += 1
                st.consecutive_fail = 0

                # Server-declared limits — always adopted when present.
                hints = _parse_ratelimit_headers(headers)
                adopted = False
                if "ietf_limit" in hints and "ietf_window" in hints:
                    win = max(hints["ietf_window"], 0.001)
                    suggested = hints["ietf_limit"] / win
                    st.rate = max(self.min_rate, min(self.max_rate, suggested))
                    adopted = True
                elif "x_limit" in hints and "x_reset" in hints:
                    reset = hints["x_reset"] - now
                    if reset > 0:
                        suggested = hints["x_limit"] / reset
                        st.rate = max(self.min_rate, min(self.max_rate, suggested))
                        adopted = True

                # Heuristic climb — OFF unless explicitly enabled.
                # A polite crawler should never exceed the configured rate
                # without the server *inviting* it via headers.
                if (not adopted) and self.allow_rate_climb and st.consecutive_ok >= 5:
                    st.rate = min(self.max_rate, st.rate * 1.05)
                    st.consecutive_ok = 0

            self._persist(host)

    # ---- introspection ----

    def state(self, url_or_host: str) -> dict:
        with self._lock:
            return self._get(_host_of(url_or_host)).to_dict()

    def all_states(self) -> dict[str, dict]:
        with self._lock:
            return {h: s.to_dict() for h, s in self._states.items()}

    def reset(self, url_or_host: Optional[str] = None) -> None:
        with self._lock:
            if url_or_host is None:
                self._states.clear()
            else:
                self._states.pop(_host_of(url_or_host), None)

    # ---- proxy hook ----

    def proxy_for(self, url_or_host: str) -> Optional[str]:
        if self.proxy_provider is None:
            return None
        try:
            return self.proxy_provider(_host_of(url_or_host))
        except Exception as e:  # noqa: BLE001
            self._log.warn("proxy_provider raised", error=str(e))
            return None

    # ---- convenience request ----

    async def request(self, session: Any, method: str, url: str, **kwargs: Any) -> Any:
        await self.acquire(url)
        proxy = self.proxy_for(url)
        if proxy and "proxy" not in kwargs:
            kwargs["proxy"] = proxy
        with Timer(f"request.{method}.{_host_of(url)}", logger=self._log):
            resp = await session.request(method, url, **kwargs)
        self.observe(url, resp.status_code, dict(resp.headers))
        return resp

    async def __aenter__(self) -> "RateLimiter":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


# --- Global convenience ------------------------------------------------------

_g_lock = threading.Lock()
_g_limiter: Optional[RateLimiter] = None


def get_limiter() -> RateLimiter:
    global _g_limiter
    with _g_lock:
        if _g_limiter is None:
            _g_limiter = RateLimiter()
        return _g_limiter


def reset_limiters() -> None:
    global _g_limiter
    with _g_lock:
        _g_limiter = None