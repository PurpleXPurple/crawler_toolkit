<div align="center">

<img src="assets/header.svg" alt="crawler_toolkit" width="100%"/>

[![Python](https://img.shields.io/badge/python-3.10%2B-4a9eff?style=for-the-badge&logo=python&logoColor=white&labelColor=0a0e27)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-a855f7?style=for-the-badge&logo=opensourceinitiative&logoColor=white&labelColor=0a0e27)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.1.0-ec4899?style=for-the-badge&labelColor=0a0e27)](CHANGELOG.md)

[![Tests](https://img.shields.io/github/actions/workflow/status/your-handle/crawler_toolkit/test.yml?style=for-the-badge&label=tests&labelColor=0a0e27&color=22d3ee)](https://github.com/your-handle/crawler_toolkit/actions)
[![Lint](https://img.shields.io/github/actions/workflow/status/your-handle/crawler_toolkit/lint.yml?style=for-the-badge&label=lint&labelColor=0a0e27&color=22d3ee)](https://github.com/your-handle/crawler_toolkit/actions)

[![curl_cffi](https://img.shields.io/badge/curl__cffi-0.6%2B-4a9eff?style=for-the-badge&labelColor=0a0e27)](https://github.com/yifeikong/curl_cffi)
[![selectolax](https://img.shields.io/badge/selectolax-0.3%2B-a855f7?style=for-the-badge&labelColor=0a0e27)](https://github.com/rushter/selectolax)
[![SQLite](https://img.shields.io/badge/sqlite-stdlib-ec4899?style=for-the-badge&logo=sqlite&logoColor=white&labelColor=0a0e27)](https://sqlite.org/)

<img src="assets/divider.svg" alt="" width="100%"/>

**Rate limiting. Health checks. Telemetry. Integration.**
<br/>
Four modules. One toolkit. Zero config to start.

<img src="assets/divider.svg" alt="" width="100%"/>

</div>

## Overview

crawler_toolkit is infrastructure for scrapers that use `curl_cffi` and `selectolax`. Four modules, each doing one job:

- **`imports_integration`** — lazy dependency resolution, capability reporting, one config object.
- **`info_manager`** — structured logging with dedup, a general SQLite store, self-tests.
- **`crawler_checks`** — adaptive per-host rate limiter driven by server response headers.
- **`health_checks`** — sixteen checks per URL, from status code to security headers.

You write the crawl loop. The toolkit handles the limits, the logging, and the checking.

## Install

From source (the only way right now):

```bash
git clone https://github.com/PurpleXPurple/crawler_toolkit
cd crawler_toolkit
pip install -e ".[dev]"
```

Requires Python 3.10+, `curl_cffi>=0.6.0`, `selectolax>=0.3.0`. `pytest` and `pytest-asyncio` come with the `[dev]` extra.

## Quickstart

```python
import asyncio
from crawler_toolkit.crawler_checks import RateLimiter
from crawler_toolkit.health_checks import HealthChecker
from crawler_toolkit.info_manager import get_logger
from crawler_toolkit.imports_integration import require

async def main():
    log = get_logger("app")
    limiter = RateLimiter(default_rate=1.0)
    hc = HealthChecker()
    c = require("curl_cffi")

    async with c.AsyncSession(impersonate="chrome124") as session:
        url = "https://example.com"
        await limiter.acquire(url)
        resp = await session.get(url)
        limiter.observe(url, resp.status_code, dict(resp.headers))
        log.info("fetched", url=url, status=resp.status_code)

    report = await hc.check(url)
    print(report.summary())

asyncio.run(main())
```

<div align="center">
<img src="assets/pipeline.svg" alt="module pipeline" width="100%"/>
</div>

## Modules

### `crawler_toolkit.imports_integration`

Front door. Never top-level imports the other toolkit modules.

```python
from crawler_toolkit.imports_integration import (
    require, optional, has,
    ToolkitConfig, get_config, configure, reset_config,
    capabilities, describe, public_api,
)
```

- **`require(name)`** returns the module or raises `MissingDependencyError`.
- **`optional(name)`** returns the module or `None`.
- **`has(name)`** returns a bool.
- **`configure(**kwargs)`** mutates the global config. Unknown keys raise `ToolkitError`.
- **`describe()`** prints a capability report.

Importing `crawler_toolkit` never imports `curl_cffi` or `selectolax`. They load on first access.

### `crawler_toolkit.info_manager`

Logging, telemetry, and a general SQLite store behind one facade.

```python
from crawler_toolkit.info_manager import (
    get_logger, log, query, execute,
    Timer, trace, run_self_tests, get_store, configure_logging,
    Level, Store, LogRecord,
)
```

- **Bounded-LRU dedup.** 10,000 identical log lines become one row with `count=10000`.
- **Redaction.** Values for `token`, `key`, `secret`, `password`, `auth`, `apikey`, `api_key` are scrubbed before write.
- **One SQLite file.** Tables `logs`, `telemetry`, `host_state` plus anything you create.
- **Passenger, not dependency.** If the DB can't be written, falls back to an in-memory ring buffer. Rate limiting and health checks keep working.

```python
log = get_logger("scraper")
log.info("retrying", url="https://x.com", attempt=3)

with Timer("fetch.detail"):
    resp = await session.get(url)

execute("CREATE TABLE IF NOT EXISTS pages (url TEXT, status INT, ts REAL)")
execute("INSERT INTO pages VALUES (?, ?, ?)", (url, 200, time.time()))
rows = query("SELECT * FROM pages WHERE status >= ?", (400,))

for name, (ok, detail) in run_self_tests().items():
    print(name, "OK" if ok else "FAIL")
```

### `crawler_toolkit.crawler_checks`

Adaptive per-host token bucket. State persists across process restarts.

```python
from crawler_toolkit.crawler_checks import (
    RateLimiter, HostState, get_limiter, reset_limiters,
)
```

- **Per-host.** A slow host does not slow down a fast host.
- **429** with `Retry-After` → cooldown, rate halved. **5xx** → rate drops 25%. **2xx** → holds.
- **Server-declared limits** via `RateLimit-*` or `X-RateLimit-*` are adopted outright.
- **`allow_rate_climb` is off by default.** Without a server invitation, the rate never exceeds the configured default.
- **Proxy hook.** You supply `proxy_provider(host) -> str | None`. No rotation built in.

```python
limiter = RateLimiter(default_rate=1.0)
await limiter.acquire(url)
resp = await session.get(url)
limiter.observe(url, resp.status_code, dict(resp.headers))

state = limiter.state(url)
# {"host": ..., "rate": 1.0, "tokens": 0.42, "total_requests": 47, ...}
```

| parameter | default |
|---|---|
| `default_rate` | 1.0 |
| `default_burst` | 3.0 |
| `min_rate` | 0.05 |
| `max_rate` | 20.0 |
| `max_retry_after` | 3600.0 |
| `honor_retry_after` | True |
| `allow_rate_climb` | False |

<div align="center">
<img src="assets/ratelimiter.svg" alt="rate limiter" width="80%"/>
</div>

### `crawler_toolkit.health_checks`

Sixteen checks per URL. Each returns `ok`, `message`, `severity`, `value`, `duration_ms`.

```python
from crawler_toolkit.health_checks import (
    HealthChecker, CheckResult, HealthReport, CheckContext,
    check_url, ALL_CHECKS,
)
```

| check | catches |
|---|---|
| `status` | non-2xx/3xx |
| `response_time` | slow origin |
| `ssl` | expiring or expired certs |
| `redirects` | HTTPS→HTTP downgrades, loops |
| `challenge_page` | Cloudflare, DDoS-Guard, JS-wall markers |
| `dom` | empty or malformed HTML |
| `content_hash` | change detection |
| `content_size` | truncated or bloated responses |
| `required_headers` | user-declared must-haves |
| `security_headers` | HSTS, CSP, XFO, XCTO, Referrer-Policy, Permissions-Policy |
| `cookie_flags` | missing Secure/HttpOnly/SameSite |
| `hsts` | max-age below 180 days |
| `csp` | unsafe-inline / unsafe-eval |
| `trackers` | GA, GTM, Hotjar, Mixpanel, Segment |
| `ads` | AdSense, Criteo, Taboola, Outbrain |
| `dns` | resolution failures |

```python
hc = HealthChecker()
report = await hc.check("https://example.com")
print(report.summary())

fails = [r for r in report.results if not r.ok]
for r in fails:
    print(f"{r.name}: {r.message} ({r.severity})")
```

Subset of checks:

```python
from crawler_toolkit.health_checks import HealthChecker, _check_status, _check_response_time
fast = HealthChecker(checks=[_check_status, _check_response_time])
```

## Configuration

All config lives in one dataclass. Mutate once, read anywhere.

```python
from crawler_toolkit.imports_integration import configure

configure(
    log_level="INFO",
    db_path=None,                    # None → ~/.cache/crawler_toolkit/toolkit.db
    default_rate=1.0,
    default_burst=3.0,
    honor_retry_after=True,
    allow_rate_climb=False,
    proxy_provider=None,
    impersonate="chrome124",
)
```

Unknown keys raise `ToolkitError`. Deliberate — a silent config typo is a bug that shows up three days later.

## Testing

Only one test file ships. It's a compressed audit that doubles as a pytest module and a CLI.

```bash
pytest tests/test_files.py -v
python tests/test_files.py
```

Eight sections:

1. Package layout
2. Import smoke test (every module in the package)
3. Default config audit (17 checks)
4. Format loaders (28 text formats)
5. Edge cases (empty files per format, malformed JSON/XML/TOML/CSV)
6. Concurrency (8 threads on the loader table)
7. Volume (5000-line JSONL)
8. Summary

Exit code 0 on all-pass, 1 on any failure. Usable as a pre-commit gate.

## Stress Testing

One harness ships: `stress/heavy_stress.py`. It crawls Wikipedia over real HTML (no API, no REST) using `chrome124` TLS impersonation.

```bash
python stress/heavy_stress.py
python stress/heavy_stress.py --max-requests 200 --max-seconds 120
python stress/heavy_stress.py --seeds 5 --phases warmup,seed,crawl --out smoke.jsonl
```

Six phases: `warmup`, `seed`, `crawl`, `health`, `limiter`, plus optional `network_probe`.

Output is a single JSONL file. One event per line. Every metric, every toolkit log, every telemetry row, all in one place.

```bash
grep '"event":"crawl"' heavy_stress.jsonl | python -m json.tool
grep '"event":"summary"' heavy_stress.jsonl | tail -1
```

**Ethical note.** Set `CONTACT` at the top of the file to a real email or URL before running. Wikipedia's User-Agent policy requires a contact string. The script refuses to start with the placeholder.

## Project Layout

```
crawler_toolkit/
├── crawler_toolkit/
│   ├── __init__.py
│   ├── imports_integration.py
│   ├── info_manager.py
│   ├── crawler_checks.py
│   └── health_checks.py
├── tests/
│   └── test_files.py
├── stress/
│   └── heavy_stress.py
├── assets/
│   ├── header.svg
│   ├── pipeline.svg
│   ├── ratelimiter.svg
│   ├── divider.svg
│   └── logo.svg
├── .github/
│   ├── workflows/
│   │   ├── test.yml
│   │   └── lint.yml
│   ├── ISSUE_TEMPLATE/
│   │   └── config.yml
│   ├── dependabot.yml
│   └── FUNDING.yml
├── pyproject.toml
├── CHANGELOG.md
├── LICENSE
├── README.md
└── .gitignore
```

## Design

- **Async core, sync facade.** `await limiter.acquire(url)` for async code, `limiter.acquire_sync(url)` for scripts.
- **Passengers, not dependencies.** Telemetry is a passenger: if the SQLite file can't be written, the limiter still limits and health checks still check.
- **Per-host, never global.** A DNS outage does not trigger a global backoff.
- **Conservative by default.** Rate limiter respects `Retry-After` and clamps malicious values to one hour.
- **Fail loud on the small things.** `info_manager` self-tests check dedup, redaction, and store round-trip on every `run_self_tests()` call.
- **Four modules, not fourteen.** The split is intentional. Each module does one job.

## Known Limitations

- Health checks do not feed the limiter. A 429 during a health check does not affect the limiter's learned rate.
- No cross-process coordination. Two processes hitting the same host each maintain their own in-memory state.
- `_check_ssl` and `_check_dns` use blocking socket calls inside async code. Under concurrent load they serialize the event loop.
- No automatic retries. The limiter observes responses and paces the next call. Retry policy is yours.
- No proxy rotation. You supply the pool via `proxy_provider`.

## Contributing

Issues and pull requests welcome. Before opening a PR:

1. Run `pytest tests/test_files.py` and confirm all eight sections pass.
2. Run `python tests/test_files.py` and confirm exit code 0.
3. Match the existing style. The codebase is deliberately terse.
4. Every new public function needs a test.
5. Every new config field needs a line in `ToolkitConfig` and a check in `test_files.py`.
6. Update `CHANGELOG.md` under `Unreleased`.

## License

MIT. See `LICENSE`.

<div align="center">
<img src="assets/divider.svg" alt="" width="100%"/>

**Built for scrapers that need to behave.**

<sub>If this saved you an IP ban, a star helps.</sub>

<img src="assets/divider.svg" alt="" width="100%"/>

</div>

---