"""Structured logging, telemetry, general SQLite store, debugging, self-tests.

Public surface (freeze this):
    get_logger, log, query, execute,
    Timer, trace, run_self_tests, get_store, configure_logging,
    Level, Store, LogRecord.

Everything else is internal.
"""
from __future__ import annotations

import atexit
import functools
import hashlib
import json
import re
import sqlite3
import sys
import threading
import time
import traceback
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .imports_integration import get_config

__all__ = [
    "Level", "LogRecord", "Logger", "get_logger", "log",
    "get_store", "query", "execute", "Store",
    "Timer", "trace", "run_self_tests", "configure_logging",
]


# --- Levels ------------------------------------------------------------------

class Level(IntEnum):
    TRACE = 5
    DEBUG = 10
    INFO = 20
    WARN = 30
    ERROR = 40
    FATAL = 50

    @classmethod
    def parse(cls, v: Any) -> "Level":
        if isinstance(v, Level):
            return v
        if isinstance(v, int):
            return cls(v)
        try:
            return cls[str(v).upper()]
        except KeyError:
            return cls.INFO


# --- Redaction ---------------------------------------------------------------

def _redact(value: str, keys: tuple[str, ...]) -> str:
    if not keys or not value:
        return value
    pat = "|".join(re.escape(k) for k in keys)
    rx = re.compile(rf"(?i)\b({pat})\b=([^&\s]+)")
    return rx.sub(r"\1=***", value)


# --- Deduper -----------------------------------------------------------------

class _Deduper:
    """Bounded-LRU deduper. Same key inside the window increments count."""

    def __init__(self, max_size: int = 4096, window_s: float = 60.0) -> None:
        self.max_size = max_size
        self.window_s = window_s
        self._lock = threading.Lock()
        self._map: "OrderedDict[str, list]" = OrderedDict()

    def hit(self, key: str, now: float) -> tuple[bool, int]:
        """Return (is_duplicate, count_so_far)."""
        with self._lock:
            entry = self._map.get(key)
            if entry and now - entry[0] <= self.window_s:
                entry[1] += 1
                self._map.move_to_end(key)
                return True, entry[1]
            self._map[key] = [now, 1]
            self._map.move_to_end(key)
            while len(self._map) > self.max_size:
                self._map.popitem(last=False)
            return False, 1


# --- Records -----------------------------------------------------------------

@dataclass
class LogRecord:
    ts: float
    level: Level
    module: str
    message: str
    extra: dict = field(default_factory=dict)
    count: int = 1
    dedup_key: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["level"] = int(self.level)
        d["level_name"] = self.level.name
        return d


# --- Store -------------------------------------------------------------------

class Store:
    """General-purpose SQLite store. Thread-safe. Shared across the toolkit.

    Tables created on open: meta, logs, telemetry, host_state.
    Callers may create and query their own tables freely.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: Path | str | None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._memory: list[dict] = []
        self._memory_max = 1024
        self._open()

    # ---- lifecycle ----

    def _open(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.path), check_same_thread=False, isolation_level=None,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._ensure_schema()
        except sqlite3.Error as e:
            self._conn = None
            sys.stderr.write(f"[info_manager] store unavailable: {e}\n")

    def _ensure_schema(self) -> None:
        c = self._conn
        if c is None:
            return
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                level INTEGER NOT NULL,
                module TEXT NOT NULL,
                message TEXT NOT NULL,
                extra TEXT,
                count INTEGER NOT NULL DEFAULT 1,
                dedup_key TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
            CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level);
            CREATE INDEX IF NOT EXISTS idx_logs_dedup ON logs(dedup_key);
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                key TEXT NOT NULL,
                value_num REAL,
                value_text TEXT,
                tags TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tel_key ON telemetry(key);
            CREATE TABLE IF NOT EXISTS host_state (
                host TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                updated REAL NOT NULL
            );
            """
        )
        row = c.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        if row is None:
            c.execute("INSERT INTO meta(k,v) VALUES ('schema_version', ?)",
                      (str(self.SCHEMA_VERSION),))

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    # ---- generic SQL ----

    def execute(self, sql: str, params: Iterable = ()) -> Optional[sqlite3.Cursor]:
        with self._lock:
            if self._conn is None:
                return None
            try:
                return self._conn.execute(sql, tuple(params))
            except sqlite3.Error as e:
                sys.stderr.write(f"[info_manager] SQL error: {e}\n")
                return None

    def query(self, sql: str, params: Iterable = ()) -> list[dict]:
        with self._lock:
            if self._conn is None:
                return []
            try:
                cur = self._conn.execute(sql, tuple(params))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
            except sqlite3.Error as e:
                sys.stderr.write(f"[info_manager] SQL error: {e}\n")
                return []

    # ---- log ops ----

    def insert_log(self, rec: LogRecord) -> None:
        if self._conn is None:
            self._memory.append(rec.to_dict())
            if len(self._memory) > self._memory_max:
                self._memory.pop(0)
            return
        self.execute(
            "INSERT INTO logs (ts, level, module, message, extra, count, dedup_key) "
            "VALUES (?,?,?,?,?,?,?)",
            (rec.ts, int(rec.level), rec.module, rec.message,
             json.dumps(rec.extra, default=str) if rec.extra else None,
             rec.count, rec.dedup_key),
        )

    def update_log_count(self, dedup_key: str, count: int) -> None:
        if self._conn is None:
            for entry in reversed(self._memory):
                if entry.get("dedup_key") == dedup_key:
                    entry["count"] = count
                    return
            return
        self.execute("UPDATE logs SET count=? WHERE dedup_key=?", (count, dedup_key))

    def memory_snapshot(self) -> list[dict]:
        return list(self._memory)

    # ---- telemetry ----

    def record_telemetry(self, key: str, value_num: Optional[float] = None,
                         value_text: Optional[str] = None,
                         tags: Optional[dict] = None) -> None:
        self.execute(
            "INSERT INTO telemetry (ts, key, value_num, value_text, tags) "
            "VALUES (?,?,?,?,?)",
            (time.time(), key, value_num, value_text,
             json.dumps(tags, default=str) if tags else None),
        )

    # ---- host state ----

    def save_host_state(self, host: str, state: dict) -> None:
        self.execute(
            "INSERT INTO host_state (host, state, updated) VALUES (?,?,?) "
            "ON CONFLICT(host) DO UPDATE SET state=excluded.state, updated=excluded.updated",
            (host, json.dumps(state, default=str), time.time()),
        )

    def load_host_state(self, host: str) -> Optional[dict]:
        rows = self.query("SELECT state FROM host_state WHERE host=?", (host,))
        if not rows:
            return None
        try:
            return json.loads(rows[0]["state"])
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def all_host_states(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.query("SELECT host, state FROM host_state"):
            try:
                out[r["host"]] = json.loads(r["state"])
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        return out


# --- Logger ------------------------------------------------------------------

_COLORS = {
    Level.TRACE: "\x1b[90m",
    Level.DEBUG: "\x1b[36m",
    Level.INFO:  "\x1b[32m",
    Level.WARN:  "\x1b[33m",
    Level.ERROR: "\x1b[31m",
    Level.FATAL: "\x1b[1;31m",
}
_RESET = "\x1b[0m"


class Logger:
    def __init__(self, module: str, store: Store, level: Level,
                 deduper: _Deduper, redact_keys: tuple[str, ...],
                 to_stderr: bool) -> None:
        self.module = module
        self._store = store
        self._level = level
        self._deduper = deduper
        self._redact_keys = redact_keys
        self._to_stderr = to_stderr

    def _emit(self, level: Level, msg: Any, extra: Optional[dict] = None) -> None:
        if level < self._level:
            return
        msg = _redact(str(msg), self._redact_keys)
        extra = extra or {}
        if extra:
            extra = {
                k: (_redact(v, self._redact_keys) if isinstance(v, str) else v)
                for k, v in extra.items()
            }
        now = time.time()
        src = f"{int(level)}|{self.module}|{msg}|{json.dumps(extra, sort_keys=True, default=str)}"
        key = hashlib.sha1(src.encode("utf-8", "replace")).hexdigest()
        is_dup, count = self._deduper.hit(key, now)
        if is_dup:
            self._store.update_log_count(key, count)
            return
        rec = LogRecord(ts=now, level=level, module=self.module, message=msg,
                        extra=extra, count=1, dedup_key=key)
        self._store.insert_log(rec)
        if self._to_stderr:
            self._write_stderr(rec)

    def _write_stderr(self, rec: LogRecord) -> None:
        try:
            use_color = sys.stderr.isatty()
            color = _COLORS.get(rec.level, "") if use_color else ""
            reset = _RESET if use_color else ""
            ts = time.strftime("%H:%M:%S", time.localtime(rec.ts))
            line = f"{color}{ts} {rec.level.name:5s} {rec.module}: {rec.message}{reset}"
            if rec.extra:
                line += f" {json.dumps(rec.extra, default=str)}"
            sys.stderr.write(line + "\n")
        except Exception:
            pass

    def trace(self, msg: Any, **extra: Any) -> None: self._emit(Level.TRACE, msg, extra)
    def debug(self, msg: Any, **extra: Any) -> None: self._emit(Level.DEBUG, msg, extra)
    def info(self, msg: Any, **extra: Any) -> None:  self._emit(Level.INFO, msg, extra)
    def warn(self, msg: Any, **extra: Any) -> None:  self._emit(Level.WARN, msg, extra)
    def error(self, msg: Any, **extra: Any) -> None: self._emit(Level.ERROR, msg, extra)
    def fatal(self, msg: Any, **extra: Any) -> None: self._emit(Level.FATAL, msg, extra)

    def exception(self, msg: Any, **extra: Any) -> None:
        extra = {**extra, "traceback": traceback.format_exc()}
        self._emit(Level.ERROR, msg, extra)


# --- Global store + loggers --------------------------------------------------

_lock = threading.RLock()
_store: Optional[Store] = None
_deduper = _Deduper(max_size=4096)
_loggers: dict[str, Logger] = {}


def get_store() -> Store:
    global _store
    with _lock:
        if _store is None:
            cfg = get_config()
            path = cfg.db_path or (Path.home() / ".cache" / "crawler_toolkit" / "toolkit.db")
            _store = Store(path)
        return _store


def get_logger(module: str = "toolkit") -> Logger:
    with _lock:
        lg = _loggers.get(module)
        if lg is None:
            cfg = get_config()
            lg = Logger(
                module=module,
                store=get_store(),
                level=Level.parse(cfg.log_level),
                deduper=_deduper,
                redact_keys=cfg.log_redact_keys,
                to_stderr=cfg.log_to_stderr,
            )
            _loggers[module] = lg
        return lg


def log(msg: Any, level: Any = "INFO", module: str = "toolkit", **extra: Any) -> None:
    get_logger(module)._emit(Level.parse(level), msg, extra)


def query(sql: str, params: Iterable = ()) -> list[dict]:
    return get_store().query(sql, params)


def execute(sql: str, params: Iterable = ()) -> None:
    get_store().execute(sql, params)


def configure_logging(level: Any = "INFO", to_stderr: bool = True,
                      db: Optional[Path] = None) -> None:
    cfg = get_config()
    cfg.log_level = str(level)
    cfg.log_to_stderr = to_stderr
    if db is not None:
        cfg.db_path = db
    with _lock:
        _loggers.clear()


# --- Timer + trace -----------------------------------------------------------

class Timer:
    """Context manager that logs duration and records telemetry."""

    def __init__(self, name: str, logger: Optional[Logger] = None,
                 record: bool = True, level: Any = "DEBUG") -> None:
        self.name = name
        self.logger = logger or get_logger("timer")
        self.record = record
        self.level = Level.parse(level)
        self.start = 0.0
        self.elapsed_ms = 0.0

    def __enter__(self) -> "Timer":
        self.start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.elapsed_ms = (time.perf_counter() - self.start) * 1000.0
        self.logger._emit(self.level, f"{self.name}: {self.elapsed_ms:.2f}ms")
        if self.record:
            try:
                get_store().record_telemetry(
                    f"timer.{self.name}", value_num=self.elapsed_ms,
                    tags={"exception": exc_type.__name__ if exc_type else None},
                )
            except Exception:
                pass
        return False


def trace(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        lg = get_logger(getattr(fn, "__module__", "trace"))
        with Timer(fn.__qualname__, logger=lg, level="TRACE"):
            return fn(*args, **kwargs)
    return wrapper


# --- Self-tests --------------------------------------------------------------

def run_self_tests() -> dict[str, tuple[bool, str]]:
    """Run internal integrity checks. Returns {name: (ok, detail)}."""
    results: dict[str, tuple[bool, str]] = {}

    def _run(name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
            results[name] = (True, "ok")
        except Exception as e:  # noqa: BLE001
            results[name] = (False, f"{type(e).__name__}: {e}")

    def _t_deduper() -> None:
        d = _Deduper(max_size=8, window_s=10.0)
        now = time.time()
        assert d.hit("a", now) == (False, 1)
        assert d.hit("a", now + 0.01) == (True, 2)
        assert d.hit("b", now + 0.02) == (False, 1)

    def _t_redact() -> None:
        out = _redact("https://x.com/?token=abc&foo=bar", ("token",))
        assert "abc" not in out, out
        assert "token=***" in out, out

    def _t_store_roundtrip() -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            s = Store(Path(td) / "t.db")
            try:
                rec = LogRecord(ts=time.time(), level=Level.INFO, module="t",
                                message="hello", extra={"a": 1}, dedup_key="k")
                s.insert_log(rec)
                rows = s.query("SELECT message FROM logs")
                assert rows and rows[0]["message"] == "hello"
                s.save_host_state("h", {"rate": 1.0})
                st = s.load_host_state("h")
                assert st and st["rate"] == 1.0
                s.execute("CREATE TABLE IF NOT EXISTS u (k TEXT)")
                s.execute("INSERT INTO u VALUES (?)", ("x",))
                assert s.query("SELECT * FROM u") == [{"k": "x"}]
            finally:
                s.close()

    def _t_logger() -> None:
        get_logger("selftest").debug("ping")

    _run("deduper", _t_deduper)
    _run("redact", _t_redact)
    _run("store_roundtrip", _t_store_roundtrip)
    _run("logger", _t_logger)
    return results


# --- atexit ------------------------------------------------------------------

atexit.register(lambda: _store.close() if _store is not None else None)