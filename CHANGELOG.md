# crawler_toolkit — Session Log

> Session log for the crawler_toolkit build. 3 sections only: changes, dates, edits. Brief commit-style messages.

---

## Changes

- **`superplan` v1** — 16-section master plan produced. Non-goals, threat model, phased execution.
- **`superplan` v2 (refactor)** — same content, uniform 5-field phase records. Open Decisions hoisted to top.
- **Decisions locked** — solo maintainer; general SQL; user-supplied proxies; minimal retries; pytest required; full health-check surface.
- **Scaffold** — repo tree, `pyproject.toml`, package folder, 4 empty modules.
- **`imports_integration`** — lazy `curl_cffi` / `selectolax` resolution, `ToolkitConfig`, `configure`, `describe`, `public_api`.
- **`info_manager`** — structured logger, bounded-LRU dedup, redaction hook, shared SQLite `Store`, `Timer`, `trace`, `run_self_tests`.
- **`crawler_checks`** — adaptive per-host token bucket, header parsing with clamps, per-host state persisted to SQLite, `proxy_provider` hook.
- **`health_checks`** — 16 checks per URL (status → dns).
- **Tests** — `conftest.py`, four module test files, `test_files.py`.
- **`stress/heavy_stress.py` v1** — local-fixture stress (5 phases, single JSONL output).
- **`examples/wikipedia_smoke.py`** — polite API crawl, 1 req/sec.
- **`stress/heavy_stress.py` v2** — full HTML crawl of Wikipedia, `chrome124` impersonation, browser headers, BFS with selectolax link extraction.
- **Bug sweep** — 6 findings from the first Wikipedia run (A1–A6).
- **Toolkit patches** — `allow_rate_climb` flag; `_check_dom` selector fix.
- **`test_files.py` compression** — 528 → 171 → 155 lines. CSV `strict=True` fix.

---

## Dates

| date | version | note |
|---|---|---|
| 2026-10-02 | 0.1.0 | Full build, initial test pass, first Wikipedia run |
| 2026-10-02 | 0.1.0+r1 | Post-audit patches (A1–A6), test_files compression |
| TBD | 0.2.0 | HealthChecker → limiter feedback (deferred) |

---

## Code Edits

### `crawler_toolkit/__init__.py`
- **broken** → `from .crawler_toolkit.imports_integration import ...` (nonexistent nested package).
- **fixed** → `from .imports_integration import ...`. Removed eager `from .crawler_toolkit import ...` that defeated lazy `__getattr__`.
- **why** — Pylance unresolved-import; folder was package root, not nested.

### `crawler_toolkit/imports_integration.py`
- **+ `allow_rate_climb: bool = False`** on `ToolkitConfig`.
- **why** — audit A2: limiter escalated from 1.0 → 1.55 req/sec with no server signal.

### `crawler_toolkit/crawler_checks.py`
- **+ `allow_rate_climb` param** on `__init__`, threaded to `observe`.
- **branch guard** — `if (not adopted) and self.allow_rate_climb and st.consecutive_ok >= 5:`
- **why** — heuristic climb was on by default; violiated the configured rate being a hard ceiling. Server-declared `RateLimit-*` headers still always adopted.

### `crawler_toolkit/health_checks.py`
- **`_check_dom` rewritten.**
- **before** → `body.css("> *")` (returned 0 on current selectolax).
- **after** → `tree.css("body > *")` from root, with `body.iter(include_text=False)` fallback.
- **why** — audit A5: `dom` check failed on every Wikipedia page.

### `stress/heavy_stress.py` v2
- **`_extract_links` rewritten.** Manual anchor iteration + `_normalize_wiki_href`; removed CSS attribute-selector dependency.
- **`+ diag` dict** — stage counts (`total_a`, `with_href`, `wiki_prefix`, `after_ns_filter`, …) to localize where links are lost.
- **`+ links_internal_css` / `links_total_css`** on `page_parsed` events — cross-validate filter vs. parser.
- **`p_limiter`** — `c.log("host_state", **s)` (was `host=h, **s`, collided).
- **`p_health`** — `fails` now `[{name, msg, sev}]` (was names only).
- **`p_crawl`** — `visited = set(seeds)` at start; seeds not re-fetched.
- **why** — audit A1/A3/A4/A6.

### `tests/test_files.py`
- **v1 → v2 (528 → 171)** — loader factories collapsed, fixture dict generatored, CLI section-printers looped.
- **v2 → v3 (171 → 155)** — `_r` dropped unused keys; `check_imports` early-returns before building debug string; `.log` / `.xml` branches tightened.
- **+ `strict=True`** on `csv.DictReader`.
- **why** — `test_edges` failed: CSV reader is lenient on unclosed quotes by default.

### `stress/heavy_stress.py` → `stress/fixture_stress.py`
- **renamed.** Name freed for the Wikipedia HTML crawler.
- **why** — user request; keep both, don't lose the offline stress.

### `CHANGELOG.md`, `README.md`, `pyproject.toml`, `.gitignore`
- **created**; unchanged after initial write.
- **why** — v0.1.0 baseline.

---

## Deferred

- **A13** — `HealthChecker` should feed status+headers back to `RateLimiter.observe`. Currently bypasses own learning loop. Requires `HealthChecker.check(..., limiter=...)` signature change.
- **A9** — DB persists between runs from same `--out`. Add `--fresh-db` or timestamp suffix.
- **A5 follow-through** — confirm `_check_dom` passes on Wikipedia after patch; needs rerun.
- **A1 follow-through** — confirm `warmup_link_diag` shows `wiki_prefix` high and `after_dedup` still low, or both low. Closes whether filter or parser is at fault.