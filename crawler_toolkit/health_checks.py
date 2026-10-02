"""Website health checking: 16 checks per URL.

Checks (all run by default):
    status, response_time, ssl, redirects, challenge_page,
    dom, content_hash, content_size,
    required_headers, security_headers, cookie_flags, hsts, csp,
    trackers, ads, dns.

Public surface: HealthChecker, CheckResult, HealthReport, CheckContext,
check_url, ALL_CHECKS.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import socket
import ssl
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

from .crawler_checks import _host_of, get_limiter
from .imports_integration import get_config, has, require
from .info_manager import get_logger, get_store

__all__ = [
    "CheckResult", "HealthReport", "CheckContext",
    "HealthChecker", "check_url", "ALL_CHECKS",
]


# --- Result types ------------------------------------------------------------

@dataclass
class CheckResult:
    name: str
    ok: bool
    message: str = ""
    value: Any = None
    severity: str = "info"   # info | low | medium | high | critical
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        v = d.get("value")
        if not isinstance(v, (str, int, float, bool, type(None))):
            d["value"] = str(v)
        return d


@dataclass
class HealthReport:
    url: str
    ts: float
    status: int
    results: list[CheckResult]
    duration_ms: float
    ok: bool = True

    def to_dict(self) -> dict:
        return {
            "url": self.url, "ts": self.ts, "status": self.status,
            "duration_ms": self.duration_ms, "ok": self.ok,
            "results": [r.to_dict() for r in self.results],
        }

    def summary(self) -> str:
        lines = [
            f"HealthReport {self.url} [{self.status}] "
            f"{self.duration_ms:.0f}ms ok={self.ok}"
        ]
        for r in self.results:
            mark = "PASS" if r.ok else "FAIL"
            lines.append(f"  [{mark}] {r.name}: {r.message}")
        return "\n".join(lines)


@dataclass
class CheckContext:
    url: str
    status: int
    headers: dict          # lowercased
    body: bytes
    text: str
    elapsed_ms: float
    cfg: Any
    session_response: Any = None


CheckFn = Callable[[CheckContext], CheckResult]


# --- Checks ------------------------------------------------------------------

def _check_status(ctx: CheckContext) -> CheckResult:
    s = ctx.status
    if 200 <= s < 300:
        return CheckResult("status", True, str(s), value=s)
    if 300 <= s < 400:
        return CheckResult("status", True, f"{s} (redirect)", value=s, severity="low")
    if 400 <= s < 500:
        return CheckResult("status", False, str(s), value=s, severity="medium")
    return CheckResult("status", False, str(s), value=s, severity="high")


def _check_response_time(ctx: CheckContext) -> CheckResult:
    ms = ctx.elapsed_ms
    if ms <= ctx.cfg.response_time_warn_ms:
        return CheckResult("response_time", True, f"{ms:.0f}ms", value=ms)
    if ms <= ctx.cfg.response_time_fail_ms:
        return CheckResult("response_time", False, f"{ms:.0f}ms (slow)",
                           value=ms, severity="low")
    return CheckResult("response_time", False, f"{ms:.0f}ms (very slow)",
                       value=ms, severity="medium")


def _check_ssl(ctx: CheckContext) -> CheckResult:
    if urlparse(ctx.url).scheme != "https":
        return CheckResult("ssl", True, "not https")
    host = urlparse(ctx.url).hostname
    if not host:
        return CheckResult("ssl", False, "no hostname", severity="medium")
    try:
        c = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=5) as sock:
            with c.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
                not_after = cert.get("notAfter") if cert else None
                if not not_after:
                    return CheckResult("ssl", True, "handshake ok")
                exp = datetime.strptime(
                    not_after, "%b %d %H:%M:%S %Y %Z"
                ).replace(tzinfo=timezone.utc)
                days = (exp - datetime.now(timezone.utc)).days
                if days < 0:
                    return CheckResult("ssl", False, f"expired {-days}d ago",
                                       value=days, severity="critical")
                if days < 14:
                    return CheckResult("ssl", False, f"expires in {days}d",
                                       value=days, severity="medium")
                return CheckResult("ssl", True, f"valid, {days}d left", value=days)
    except Exception as e:  # noqa: BLE001
        return CheckResult("ssl", False, f"{type(e).__name__}: {e}", severity="high")


def _check_redirects(ctx: CheckContext) -> CheckResult:
    resp = ctx.session_response
    if resp is None:
        return CheckResult("redirects", True, "no response object")
    history = getattr(resp, "history", None) or []
    final = str(getattr(resp, "url", ctx.url))
    hops = len(history)
    start_scheme = urlparse(ctx.url).scheme
    end_scheme = urlparse(final).scheme
    if start_scheme == "https" and end_scheme == "http":
        return CheckResult("redirects", False,
                           f"https→http downgrade after {hops} hops",
                           value={"hops": hops, "final": final},
                           severity="high")
    if hops > 5:
        return CheckResult("redirects", False, f"{hops} hops",
                           value={"hops": hops, "final": final},
                           severity="low")
    return CheckResult("redirects", True, f"{hops} hops",
                       value={"hops": hops, "final": final})


def _check_challenge(ctx: CheckContext) -> CheckResult:
    head = ctx.text[:5000].lower() if ctx.text else ""
    for m in ctx.cfg.challenge_markers:
        if m in head:
            return CheckResult("challenge_page", False, f"marker '{m}'",
                               value=m, severity="high")
    if ctx.status in (403, 503) and len(ctx.body) < 5000:
        return CheckResult("challenge_page", False,
                           f"{ctx.status} + short body",
                           value=len(ctx.body), severity="medium")
    return CheckResult("challenge_page", True, "no challenge markers")


def _check_dom(ctx: CheckContext) -> CheckResult:
    if not has("selectolax"):
        return CheckResult("dom", True, "selectolax missing (skipped)")
    if not ctx.text:
        return CheckResult("dom", False, "empty body", severity="medium")
    try:
        require("selectolax")
        from selectolax.parser import HTMLParser
        tree = HTMLParser(ctx.text)
        body = tree.body
        if body is None:
            return CheckResult("dom", False, "no <body>", severity="medium")
        # Primary: CSS from the tree root. `> *` on the body node returns
        # empty in current selectolax builds; `body > *` from the root works.
        children = tree.css("body > *")
        n = len(children)
        if n == 0:
            # Fallback: direct-children traversal, no selector.
            n = sum(1 for _ in body.iter(include_text=False))
        if n == 0:
            return CheckResult("dom", False, "0 top-level nodes under <body>",
                               value=0, severity="medium")
        return CheckResult("dom", True, f"{n} top-level nodes", value=n)
    except Exception as e:  # noqa: BLE001
        return CheckResult("dom", False, f"parse failed: {e}", severity="medium")


def _check_content_hash(ctx: CheckContext) -> CheckResult:
    h = hashlib.sha256(ctx.body or b"").hexdigest()[:16]
    return CheckResult("content_hash", True, h, value=h)


def _check_content_size(ctx: CheckContext) -> CheckResult:
    n = len(ctx.body or b"")
    if n < ctx.cfg.content_size_min:
        return CheckResult("content_size", False, f"{n}B (too small)",
                           value=n, severity="low")
    if n > ctx.cfg.content_size_max:
        return CheckResult("content_size", False, f"{n}B (too large)",
                           value=n, severity="low")
    return CheckResult("content_size", True, f"{n}B", value=n)


def _check_required_headers(ctx: CheckContext) -> CheckResult:
    required = ctx.cfg.required_headers
    if not required:
        return CheckResult("required_headers", True, "none required")
    missing = [h for h in required if h.lower() not in ctx.headers]
    if missing:
        return CheckResult("required_headers", False, f"missing: {missing}",
                           value=missing, severity="medium")
    return CheckResult("required_headers", True, f"{len(required)} present")


def _check_security_headers(ctx: CheckContext) -> CheckResult:
    want = ctx.cfg.security_headers
    present = [h for h in want if h in ctx.headers]
    missing = [h for h in want if h not in ctx.headers]
    if missing:
        sev = "high" if len(missing) >= 3 else "medium"
        return CheckResult("security_headers", False,
                           f"missing {len(missing)}/{len(want)}",
                           value={"present": present, "missing": missing},
                           severity=sev)
    return CheckResult("security_headers", True, "all present")


def _check_cookie_flags(ctx: CheckContext) -> CheckResult:
    set_cookie = ctx.headers.get("set-cookie")
    if not set_cookie:
        return CheckResult("cookie_flags", True, "no cookies set")
    parts = [p.strip().lower() for p in set_cookie.split(";")]
    issues: list[str] = []
    if urlparse(ctx.url).scheme == "https" and "secure" not in parts:
        issues.append("missing Secure")
    if "httponly" not in parts:
        issues.append("missing HttpOnly")
    if not any(p.startswith("samesite") for p in parts):
        issues.append("missing SameSite")
    if issues:
        return CheckResult("cookie_flags", False, ", ".join(issues),
                           value=issues, severity="low")
    return CheckResult("cookie_flags", True, "flags ok")


def _check_hsts(ctx: CheckContext) -> CheckResult:
    if urlparse(ctx.url).scheme != "https":
        return CheckResult("hsts", True, "not https")
    hsts = ctx.headers.get("strict-transport-security")
    if not hsts:
        return CheckResult("hsts", False, "missing HSTS", severity="medium")
    m = re.search(r"max-age=(\d+)", hsts)
    if not m:
        return CheckResult("hsts", False, "no max-age", value=hsts, severity="low")
    age = int(m.group(1))
    if age < 15_552_000:
        return CheckResult("hsts", False, f"max-age={age} < 180d",
                           value=age, severity="low")
    return CheckResult("hsts", True, f"max-age={age}", value=age)


def _check_csp(ctx: CheckContext) -> CheckResult:
    csp = ctx.headers.get("content-security-policy")
    if not csp:
        return CheckResult("csp", False, "missing CSP", severity="medium")
    weak: list[str] = []
    if "unsafe-inline" in csp:
        weak.append("unsafe-inline")
    if "unsafe-eval" in csp:
        weak.append("unsafe-eval")
    if weak:
        return CheckResult("csp", False, f"weak: {weak}", value=weak, severity="low")
    return CheckResult("csp", True, "present, no unsafe directives")


def _check_trackers(ctx: CheckContext) -> CheckResult:
    text = ctx.text.lower() if ctx.text else ""
    found = [m for m in ctx.cfg.tracker_markers if m in text]
    return CheckResult("trackers", True, f"{len(found)} trackers",
                       value=found, severity="info" if not found else "low")


def _check_ads(ctx: CheckContext) -> CheckResult:
    text = ctx.text.lower() if ctx.text else ""
    found = [m for m in ctx.cfg.ad_markers if m in text]
    return CheckResult("ads", True, f"{len(found)} ad networks",
                       value=found, severity="info" if not found else "low")


def _check_dns(ctx: CheckContext) -> CheckResult:
    host = urlparse(ctx.url).hostname
    if not host:
        return CheckResult("dns", False, "no hostname", severity="high")
    try:
        ip = socket.gethostbyname(host)
        return CheckResult("dns", True, f"{host} → {ip}", value=ip)
    except Exception as e:  # noqa: BLE001
        return CheckResult("dns", False, f"{type(e).__name__}: {e}", severity="high")


ALL_CHECKS: list[CheckFn] = [
    _check_status,
    _check_response_time,
    _check_ssl,
    _check_redirects,
    _check_challenge,
    _check_dom,
    _check_content_hash,
    _check_content_size,
    _check_required_headers,
    _check_security_headers,
    _check_cookie_flags,
    _check_hsts,
    _check_csp,
    _check_trackers,
    _check_ads,
    _check_dns,
]


# --- Runner ------------------------------------------------------------------

class HealthChecker:
    """Run all checks against a URL.

    Args:
        checks: override the default check list.
        impersonate: curl_cffi impersonation target (default from config).
        timeout: per-request timeout in seconds.
        proxy: static proxy URL applied to every request.
        proxy_provider: callable(host) -> proxy URL or None (fallback).
    """

    def __init__(self,
                 checks: Optional[Iterable[CheckFn]] = None,
                 impersonate: Optional[str] = None,
                 timeout: float = 20.0,
                 proxy: Optional[str] = None,
                 proxy_provider: Optional[Callable[[str], Optional[str]]] = None) -> None:
        cfg = get_config()
        self.checks = list(checks) if checks is not None else list(ALL_CHECKS)
        self.impersonate = impersonate or cfg.impersonate
        self.timeout = timeout
        self.proxy = proxy
        self.proxy_provider = proxy_provider
        self._log = get_logger("health_checks")

    async def check(self, url: str) -> HealthReport:
        limiter = get_limiter()
        await limiter.acquire(url)

        proxy = self.proxy
        if proxy is None and self.proxy_provider is not None:
            proxy = self.proxy_provider(_host_of(url))
        if proxy is None:
            proxy = limiter.proxy_for(url)

        c = require("curl_cffi")
        start = time.perf_counter()
        status = 0
        headers: dict = {}
        body = b""
        text = ""
        resp = None

        try:
            async with c.AsyncSession(impersonate=self.impersonate) as session:
                kwargs: dict = {"timeout": self.timeout}
                if proxy:
                    kwargs["proxy"] = proxy
                resp = await session.get(url, **kwargs)
                status = resp.status_code
                headers = {k.lower(): v for k, v in resp.headers.items()}
                body = resp.content or b""
                try:
                    text = resp.text
                except Exception:
                    text = body.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            self._log.error("check request failed", url=url, error=str(e))
            return HealthReport(
                url=url, ts=time.time(), status=0,
                results=[CheckResult("request", False,
                                     f"{type(e).__name__}: {e}",
                                     severity="critical")],
                duration_ms=(time.perf_counter() - start) * 1000,
                ok=False,
            )

        elapsed_ms = (time.perf_counter() - start) * 1000
        limiter.observe(url, status, headers)

        ctx = CheckContext(url=url, status=status, headers=headers,
                           body=body, text=text, elapsed_ms=elapsed_ms,
                           cfg=get_config(), session_response=resp)

        results: list[CheckResult] = []
        for fn in self.checks:
            t0 = time.perf_counter()
            try:
                r = fn(ctx)
            except Exception as e:  # noqa: BLE001
                r = CheckResult(fn.__name__, False,
                                f"{type(e).__name__}: {e}", severity="high")
            r.duration_ms = (time.perf_counter() - t0) * 1000
            results.append(r)

        ok = all(r.ok for r in results if r.severity != "info")
        report = HealthReport(url=url, ts=time.time(), status=status,
                              results=results, duration_ms=elapsed_ms, ok=ok)
        try:
            get_store().record_telemetry(
                "health.check", value_num=elapsed_ms,
                tags={"url": url, "status": status, "ok": ok},
            )
        except Exception:
            pass
        return report

    def check_sync(self, url: str) -> HealthReport:
        return asyncio.run(self.check(url))


async def check_url(url: str, **kw: Any) -> HealthReport:
    return await HealthChecker(**kw).check(url)