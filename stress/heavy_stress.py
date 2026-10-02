"""heavy_stress.py — bounded HTML crawl against Wikipedia.

Real scrape: no API, no REST. Raw HTML pages via curl_cffi with chrome124
TLS impersonation + full browser header set. selectolax for link extraction
and DOM parsing. Full 16-check HealthChecker at end.

Posture:
    rate        1 req/sec (Wikipedia polite floor)
    concurrent  8
    max         200 requests OR 120 seconds, whichever first
    UA          descriptive IF CONTACT is set; browser UA if CONTACT = None

Output: one JSONL file, one event per line.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# EDIT THIS
# ---------------------------------------------------------------------------
CONTACT: Optional[str] = "https://github.com/your-handle"
# ---------------------------------------------------------------------------

WIKI_HOST = "en.wikipedia.org"
WIKI_BASE = f"https://{WIKI_HOST}"

if CONTACT:
    USER_AGENT = f"crawler_toolkit-heavy/0.1 ({CONTACT})"
else:
    USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36")

BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
    "DNT": "1",
}


def subseq_headers(referer: str) -> dict[str, str]:
    h = dict(BROWSER_HEADERS)
    h["Sec-Fetch-Site"] = "same-origin"
    h["Referer"] = referer
    return h


# --- event log --------------------------------------------------------------

class Log:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        self._fh = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def __call__(self, event: str, **f: Any) -> None:
        rec = {"ts": round(time.time(), 6), "event": event, **f}
        with self._lock:
            self._fh.write(json.dumps(rec, default=str) + "\n")
            self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def summarize(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    s = sorted(xs)
    pick = lambda p: s[max(0, min(len(s) - 1,
                                   int(round(p / 100 * (len(s) - 1)))))]
    return {"n": len(s), "p50": round(pick(50), 2), "p95": round(pick(95), 2),
            "p99": round(pick(99), 2), "max": round(s[-1], 2),
            "mean": round(statistics.fmean(s), 2)}


# --- fetch primitive --------------------------------------------------------

async def _get_html(session, limiter, url: str,
                    referer: Optional[str] = None,
                    timeout: float = 20.0) -> tuple:
    """Return (status, text, body_bytes, ms, final_url, err)."""
    await limiter.acquire(url)
    hdrs = subseq_headers(referer) if referer else BROWSER_HEADERS
    t0 = time.perf_counter()
    try:
        r = await session.get(url, timeout=timeout, headers=hdrs)
        ms = (time.perf_counter() - t0) * 1000
        limiter.observe(url, r.status_code, dict(r.headers))
        body = r.content or b""
        try:
            text = r.text
        except Exception:
            text = body.decode("utf-8", "replace")
        final = str(getattr(r, "url", url))
        return r.status_code, text, body, ms, final, None
    except Exception as e:
        return 0, "", b"", (time.perf_counter() - t0) * 1000, url, \
               f"{type(e).__name__}: {e}"


# --- DOM helpers ------------------------------------------------------------

_SKIP_NAMESPACES = (
    "/wiki/Special:", "/wiki/Talk:", "/wiki/User:", "/wiki/User_talk:",
    "/wiki/Help:", "/wiki/Help_talk:", "/wiki/Wikipedia:",
    "/wiki/Wikipedia_talk:", "/wiki/Template:", "/wiki/Template_talk:",
    "/wiki/Category:", "/wiki/File:", "/wiki/File_talk:", "/wiki/Portal:",
    "/wiki/Draft:", "/wiki/Module:", "/wiki/MediaWiki:",
    "/wiki/Book:", "/wiki/Education_Program:", "/wiki/TimedText:",
)


def _normalize_wiki_href(href: str) -> Optional[str]:
    """Return a /wiki/... path from absolute or relative href, or None."""
    if not href:
        return None
    if href.startswith("/wiki/"):
        return href
    if "/wiki/" in href and ("wikipedia.org" in href or href.startswith("//")):
        tail = href.split("/wiki/", 1)[1]
        return "/wiki/" + tail
    return None


def _extract_links(html: str) -> tuple[list[str], dict]:
    """Return (links, diagnostics). Diagnostics show stage-by-stage counts."""
    diag = {
        "total_a": 0, "with_href": 0, "wiki_prefix": 0,
        "after_ns_filter": 0, "after_frag_strip": 0,
        "after_len_filter": 0, "after_colon_filter": 0,
        "after_dedup": 0,
    }
    try:
        from selectolax.parser import HTMLParser
    except Exception as e:
        diag["import_error"] = str(e)
        return [], diag
    try:
        tree = HTMLParser(html)
    except Exception as e:
        diag["parse_error"] = str(e)
        return [], diag

    out: list[str] = []
    seen: set[str] = set()

    # Iterate all anchors manually; do not rely on CSS attribute selectors
    # whose behavior varies across selectolax versions.
    for a in tree.css("a"):
        diag["total_a"] += 1
        href = a.attributes.get("href") or ""
        if not href:
            continue
        diag["with_href"] += 1

        path = _normalize_wiki_href(href)
        if not path:
            continue
        diag["wiki_prefix"] += 1

        if any(path.startswith(p) for p in _SKIP_NAMESPACES):
            continue
        diag["after_ns_filter"] += 1

        if "#" in path:
            path = path.split("#", 1)[0]
        diag["after_frag_strip"] += 1

        if len(path) <= len("/wiki/"):
            continue
        diag["after_len_filter"] += 1

        tail = path.split("/wiki/", 1)[1]
        if ":" in tail:
            continue
        diag["after_colon_filter"] += 1

        full = urljoin(WIKI_BASE, path)
        if full in seen:
            continue
        seen.add(full)
        out.append(full)
        diag["after_dedup"] += 1

    return out, diag


def _extract_page_meta(html: str) -> dict:
    try:
        from selectolax.parser import HTMLParser
    except Exception:
        return {}
    try:
        tree = HTMLParser(html)
    except Exception:
        return {}
    out: dict = {}
    h1 = tree.css_first("h1#firstHeading")
    if h1:
        out["title"] = h1.text(strip=True)
    cats = tree.css("#catlinks a")
    out["categories"] = [c.text(strip=True) for c in cats if c.text(strip=True)]
    out["headings"] = len(tree.css("h2"))
    out["images"] = len(tree.css("img"))
    # Cross-validation counters. If links_internal_css is high but
    # _extract_links returned few, the filter is at fault. If both are low,
    # the parser is at fault.
    out["links_internal_css"] = len(tree.css("a[href^='/wiki/']"))
    out["links_total_css"] = len(tree.css("a[href]"))
    return out


def _extract_canonical(html: str) -> Optional[str]:
    try:
        from selectolax.parser import HTMLParser
        link = HTMLParser(html).css_first("link[rel='canonical']")
        if link:
            return link.attributes.get("href")
    except Exception:
        pass
    return None


# --- context ----------------------------------------------------------------

@dataclass
class Ctx:
    log: Log
    rate: float = 1.0
    concurrent: int = 8
    max_requests: int = 200
    max_seconds: float = 120.0
    max_depth: int = 2
    seeds: int = 20
    fanout: int = 15
    seed_urls: list[str] = field(default_factory=list)


def _limiter(rate: float = 1.0):
    from crawler_toolkit.crawler_checks import RateLimiter
    return RateLimiter(default_rate=rate, default_burst=1.0,
                       min_rate=0.1, max_rate=2.0,
                       allow_rate_climb=False)


_deadline: list[float] = [0.0]


# --- phases -----------------------------------------------------------------

async def p_warmup(c: Ctx) -> dict:
    from crawler_toolkit.imports_integration import require
    lim = _limiter(c.rate)
    async with require("curl_cffi").AsyncSession(impersonate="chrome124") as s:
        st, text, body, ms, final, err = await _get_html(
            s, lim, f"{WIKI_BASE}/wiki/Main_Page")
    ok = 200 <= st < 300
    c.log("warmup", status=st, ok=ok, ms=round(ms, 1),
          bytes=len(body), final=final, err=err)
    if ok and text:
        links, diag = _extract_links(text)
        c.log("warmup_link_diag", kept=len(links), **diag)
    return {"ok": ok, "status": st}


async def p_seed(c: Ctx) -> dict:
    from crawler_toolkit.imports_integration import require
    lim = _limiter(c.rate)
    out: list[str] = []
    async with require("curl_cffi").AsyncSession(impersonate="chrome124") as s:
        for i in range(c.seeds):
            st, text, body, ms, final, err = await _get_html(
                s, lim, f"{WIKI_BASE}/wiki/Special:Random")
            url = _extract_canonical(text) or final
            if 200 <= st < 300 and url and "/wiki/" in url and url not in out:
                out.append(url)
            c.log("seed_fetch", i=i, status=st, url=url, err=err)
    c.seed_urls = out
    c.log("seed_done", count=len(out), requested=c.seeds)
    return {"urls": out}


async def p_crawl(c: Ctx) -> dict:
    from crawler_toolkit.imports_integration import require
    lim = _limiter(c.rate)

    queue: asyncio.Queue = asyncio.Queue()
    seeds = c.seed_urls or [f"{WIKI_BASE}/wiki/Main_Page"]
    for u in seeds:
        queue.put_nowait((u, 0))

    # Seeds are already fetched by p_seed — do not re-fetch them.
    visited: set[str] = set(seeds)
    latencies: list[float] = []
    statuses: Counter = Counter()
    errors: Counter = Counter()
    link_counts: list[int] = []
    diag_totals: Counter = Counter()
    page_samples: list[dict] = []
    lock = asyncio.Lock()
    counter = {"n": 0}
    stop = {"v": False}

    async def worker():
        async with require("curl_cffi").AsyncSession(impersonate="chrome124") as s:
            while not stop["v"]:
                async with lock:
                    if counter["n"] >= c.max_requests or time.time() >= _deadline[0]:
                        stop["v"] = True
                        return
                    try:
                        url, depth = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        stop["v"] = True
                        return
                    if url in visited:
                        continue
                    visited.add(url)
                    counter["n"] += 1

                st, text, body, ms, final, err = await _get_html(
                    s, lim, url, referer=WIKI_BASE)

                links: list[str] = []
                diag: dict = {}
                meta: dict = {}
                if 200 <= st < 300:
                    links, diag = _extract_links(text)
                    meta = _extract_page_meta(text)

                async with lock:
                    latencies.append(ms)
                    if 200 <= st < 300:
                        statuses[st] += 1
                        link_counts.append(len(links))
                        for k, v in diag.items():
                            if isinstance(v, int):
                                diag_totals[k] += v
                        if len(page_samples) < 50:
                            page_samples.append({
                                "url": url, "final": final,
                                "title": meta.get("title"),
                                "headings": meta.get("headings"),
                                "images": meta.get("images"),
                                "categories": len(meta.get("categories", [])),
                                "links": len(links),
                                "links_internal_css": meta.get("links_internal_css"),
                                "links_total_css": meta.get("links_total_css"),
                                "bytes": len(body),
                            })
                    else:
                        errors[str(st) if st else (err or "exc")[:60]] += 1
                    if depth < c.max_depth:
                        for link in links[:c.fanout]:
                            if link not in visited:
                                queue.put_nowait((link, depth + 1))

    _deadline[0] = time.time() + c.max_seconds
    t0 = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(c.concurrent)))
    wall = time.perf_counter() - t0

    total = sum(statuses.values()) + sum(errors.values())
    c.log("crawl", total=total, visited=len(visited), wall=round(wall, 2),
          rps=round(total / wall, 2) if wall else 0,
          latency=summarize(latencies),
          statuses=dict(statuses), errors=dict(errors),
          links_extracted=sum(link_counts),
          avg_links=round(statistics.fmean(link_counts), 1) if link_counts else 0,
          queue_drained=queue.empty(),
          diag_totals=dict(diag_totals))
    for p in page_samples:
        c.log("page_parsed", **p)
    return {"total": total, "visited": len(visited), "wall": wall}


async def p_health(c: Ctx) -> dict:
    from crawler_toolkit.health_checks import HealthChecker
    lim = _limiter(c.rate)
    hc = HealthChecker(timeout=20.0)
    urls = (c.seed_urls or [f"{WIKI_BASE}/wiki/Main_Page"])[:3]
    for url in urls:
        await lim.acquire(url)
        r = await hc.check(url)
        fails = [{"name": x.name, "msg": x.message, "sev": x.severity}
                 for x in r.results if not x.ok]
        c.log("health", url=url, ok=r.ok, status=r.status,
              ms=round(r.duration_ms, 1), fails=fails)
    return {"urls": len(urls)}


async def p_limiter(c: Ctx) -> dict:
    st = _limiter(c.rate).all_states()
    for h, s in st.items():
        # `s` already contains a "host" key — do not pass it twice.
        c.log("host_state", **s)
    return {"hosts": len(st)}


PHASES = {
    "warmup": p_warmup,
    "seed": p_seed,
    "crawl": p_crawl,
    "health": p_health,
    "limiter": p_limiter,
}


# --- toolkit dump -----------------------------------------------------------

def _dump(log: Log) -> None:
    try:
        from crawler_toolkit.info_manager import get_store
        store = get_store()
        for t in ("logs", "telemetry", "host_state"):
            try:
                log("toolkit_dump", table=t,
                    sample=store.query(f"SELECT * FROM {t} LIMIT 2000"))
            except Exception as e:
                log("toolkit_dump", table=t, error=str(e))
    except Exception as e:
        log("toolkit_dump", error=str(e))


# --- runner -----------------------------------------------------------------

async def run(args: argparse.Namespace, log: Log) -> int:
    from crawler_toolkit.imports_integration import configure
    configure(log_level=args.log_level, default_rate=args.rate,
              default_burst=1.0, log_to_stderr=False,
              allow_rate_climb=False,
              db_path=Path(args.out).with_suffix(".db"))

    names = [n.strip() for n in args.phases.split(",") if n.strip()]
    unknown = [n for n in names if n not in PHASES]
    if unknown:
        print(f"ERROR: unknown phases: {unknown}")
        print(f"       valid: {list(PHASES)}")
        return 2
    names = [n for n in names if n in PHASES]

    ctx = Ctx(log=log, rate=args.rate, concurrent=args.concurrent,
              max_requests=args.max_requests, max_seconds=args.max_seconds,
              max_depth=args.max_depth, seeds=args.seeds, fanout=args.fanout)

    log("run_start", target=WIKI_HOST, rate=args.rate,
        concurrent=args.concurrent, max_requests=args.max_requests,
        max_seconds=args.max_seconds, max_depth=args.max_depth,
        phases=names, ua=USER_AGENT, spoofing=(CONTACT is None),
        allow_rate_climb=False)

    t0 = time.perf_counter()
    code = 0
    for name in names:
        log("phase_start", phase=name)
        ts = time.perf_counter()
        try:
            result = await PHASES[name](ctx)
            ok = True
        except Exception as e:
            result, ok = {"error": f"{type(e).__name__}: {e}"}, False
            code = 1
        log("phase_end", phase=name, ok=ok,
            wall=round(time.perf_counter() - ts, 2), result=result)

    log("run_end", wall=round(time.perf_counter() - t0, 2), code=code)
    _dump(log)
    log("summary", code=code, out=args.out)
    return code


def _args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--rate", type=float, default=1.0,
                   help="req/sec (Wikipedia polite floor: 1.0)")
    p.add_argument("--concurrent", type=int, default=8)
    p.add_argument("--max-requests", type=int, default=200)
    p.add_argument("--max-seconds", type=float, default=120.0)
    p.add_argument("--max-depth", type=int, default=2)
    p.add_argument("--seeds", type=int, default=20,
                   help="Special:Random fetches in the seed phase")
    p.add_argument("--fanout", type=int, default=15,
                   help="max new links enqueued per page")
    p.add_argument("--phases", default=",".join(PHASES))
    p.add_argument("--out", default="heavy_stress.jsonl")
    p.add_argument("--log-level", default="WARN")
    a = p.parse_args(argv)
    a.concurrent = max(1, min(a.concurrent, 32))
    a.rate = max(0.1, min(a.rate, 5.0))
    a.max_requests = max(5, a.max_requests)
    a.max_seconds = max(10.0, a.max_seconds)
    a.max_depth = max(0, min(a.max_depth, 4))
    a.seeds = max(1, min(a.seeds, 100))
    a.fanout = max(1, min(a.fanout, 50))
    return a


def main(argv=None) -> int:
    a = _args(argv)
    if CONTACT is None:
        print("WARNING: CONTACT is None — using browser UA (UA spoofing).")
        print("         Wikipedia WMF policy asks for a contact identifier.")
        print("         A URL or GitHub handle is sufficient. Continuing in 3s.")
        time.sleep(3)
    log = Log(Path(a.out))
    try:
        return asyncio.run(run(a, log))
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    finally:
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())