"""Lazy dependency resolution, capability negotiation, config surface.

This module is the toolkit's front door. It never top-level-imports the other
toolkit modules. Its job: answer, at any point, what is available, what is
missing, and how to customize the toolkit.
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = [
    "HAS_CURL_CFFI",
    "HAS_SELECTOLAX",
    "curl_cffi",
    "selectolax",
    "ToolkitError",
    "MissingDependencyError",
    "require",
    "optional",
    "has",
    "ToolkitConfig",
    "get_config",
    "configure",
    "reset_config",
    "describe",
    "capabilities",
    "public_api",
]


# --- Lazy dependency resolution ---------------------------------------------

def _try_import(name: str) -> Optional[Any]:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


_curl_cffi = _try_import("curl_cffi")
_selectolax = _try_import("selectolax")

HAS_CURL_CFFI: bool = _curl_cffi is not None
HAS_SELECTOLAX: bool = _selectolax is not None


class ToolkitError(Exception):
    """Base error for the toolkit."""


class MissingDependencyError(ToolkitError):
    """Raised when a required dependency is not installed."""

    def __init__(self, name: str, hint: str = "") -> None:
        msg = f"Required dependency '{name}' is not installed."
        if hint:
            msg += f" {hint}"
        super().__init__(msg)
        self.name = name
        self.hint = hint


_MODULES = {"curl_cffi": lambda: _curl_cffi, "selectolax": lambda: _selectolax}
_HINTS = {
    "curl_cffi": "Install with: pip install curl_cffi",
    "selectolax": "Install with: pip install selectolax",
}


def require(name: str) -> Any:
    """Return the imported module, or raise MissingDependencyError."""
    mod = _MODULES.get(name, lambda: None)()
    if mod is None:
        raise MissingDependencyError(name, _HINTS.get(name, ""))
    return mod


def optional(name: str) -> Optional[Any]:
    """Return the imported module, or None if missing."""
    return _MODULES.get(name, lambda: None)()


def has(name: str) -> bool:
    """True if the named dependency is importable."""
    return optional(name) is not None


class _LazyModule:
    """Proxy that resolves the module on attribute access."""

    def __init__(self, name: str) -> None:
        self._name = name

    def _mod(self) -> Any:
        return require(self._name)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._mod(), item)

    def __repr__(self) -> str:
        state = "available" if has(self._name) else "MISSING"
        return f"<lazy {self._name} {state}>"


curl_cffi = _LazyModule("curl_cffi")
selectolax = _LazyModule("selectolax")


# --- Config -----------------------------------------------------------------

@dataclass
class ToolkitConfig:
    """Single config surface for the whole toolkit."""

    # info_manager
    log_level: str = "INFO"
    db_path: Optional[Path] = None
    log_dedup_max: int = 4096
    log_redact_keys: tuple[str, ...] = (
        "token", "key", "secret", "password", "auth", "apikey", "api_key",
    )
    log_to_stderr: bool = True
    log_ring_size: int = 1024

    # crawler_checks
    default_rate: float = 1.0
    default_burst: float = 3.0
    min_rate: float = 0.05
    max_rate: float = 20.0
    max_retry_after: float = 3600.0
    honor_retry_after: bool = True
    allow_rate_climb: bool = False
    proxy_provider: Optional[Callable[[str], Optional[str]]] = None
    impersonate: str = "chrome124"

    # health_checks
    response_time_warn_ms: float = 1500.0
    response_time_fail_ms: float = 5000.0
    content_size_min: int = 64
    content_size_max: int = 50 * 1024 * 1024
    challenge_markers: tuple[str, ...] = (
        "just a moment", "cf-chl", "attention required",
        "checking your browser", "ddos protection by",
        "enable javascript and cookies",
    )
    tracker_markers: tuple[str, ...] = (
        "google-analytics.com", "googletagmanager.com", "doubleclick.net",
        "facebook.net", "hotjar.com", "segment.com", "mixpanel.com",
        "amplitude.com", "fullstory.com", "clarity.ms",
    )
    ad_markers: tuple[str, ...] = (
        "googlesyndication.com", "googleadservices.com", "adsystem.com",
        "adnxs.com", "criteo.com", "taboola.com", "outbrain.com",
        "pubmatic.com", "rubiconproject.com", "openx.net",
    )
    required_headers: tuple[str, ...] = ()
    security_headers: tuple[str, ...] = (
        "strict-transport-security", "content-security-policy",
        "x-content-type-options", "x-frame-options", "referrer-policy",
        "permissions-policy",
    )
    parser: str = "lexbor"

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("proxy_provider", None)
        if d.get("db_path") is not None:
            d["db_path"] = str(d["db_path"])
        return d


_config: ToolkitConfig = ToolkitConfig()


def get_config() -> ToolkitConfig:
    return _config


def configure(**kwargs: Any) -> ToolkitConfig:
    """Mutate the global config. Unknown keys raise ToolkitError."""
    valid = set(ToolkitConfig.__dataclass_fields__)
    bad = set(kwargs) - valid
    if bad:
        raise ToolkitError(f"Unknown config keys: {sorted(bad)}")
    for k, v in kwargs.items():
        setattr(_config, k, v)
    return _config


def reset_config() -> ToolkitConfig:
    global _config
    _config = ToolkitConfig()
    return _config


# --- Capability report -------------------------------------------------------

def capabilities() -> dict:
    """Return what is available in the current environment."""
    info: dict = {
        "curl_cffi": HAS_CURL_CFFI,
        "selectolax": HAS_SELECTOLAX,
        "python": sys.version.split()[0],
    }
    if HAS_CURL_CFFI:
        info["curl_cffi_version"] = getattr(_curl_cffi, "__version__", "unknown")
    if HAS_SELECTOLAX:
        info["selectolax_version"] = getattr(_selectolax, "__version__", "unknown")
    return info


def describe() -> str:
    """Human-readable capability report."""
    caps = capabilities()
    lines = ["crawler_toolkit capability report", "=" * 34]
    for k, v in caps.items():
        lines.append(f"  {k:24s} {v}")
    lines.append("")
    if not caps.get("curl_cffi"):
        lines.append("  WARNING: curl_cffi missing — network features disabled.")
    if not caps.get("selectolax"):
        lines.append("  WARNING: selectolax missing — DOM health checks disabled.")
    return "\n".join(lines)


# --- Public API discovery ----------------------------------------------------

def public_api() -> dict[str, list[str]]:
    """What each module exposes. Use to know what's available to scripts."""
    return {
        "info_manager": [
            "get_logger", "log", "query", "execute",
            "Timer", "trace", "run_self_tests", "get_store",
            "Level", "configure_logging", "Store", "LogRecord",
        ],
        "crawler_checks": [
            "RateLimiter", "HostState", "get_limiter", "reset_limiters",
        ],
        "health_checks": [
            "HealthChecker", "CheckResult", "HealthReport", "CheckContext",
            "check_url", "ALL_CHECKS",
        ],
        "imports_integration": [
            "require", "optional", "has",
            "get_config", "configure", "reset_config", "ToolkitConfig",
            "capabilities", "describe", "public_api",
        ],
    }