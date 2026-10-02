"""crawler_toolkit — rate limiting, health checks, telemetry, integration.

Top-level import is dependency-tolerant: importing this package never requires
curl_cffi or selectolax to be installed. Submodules are loaded lazily.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from .imports_integration import (
    HAS_CURL_CFFI,
    HAS_SELECTOLAX,
    ToolkitConfig,
    ToolkitError,
    MissingDependencyError,
    capabilities,
    configure,
    describe,
    get_config,
    has,
    optional,
    public_api,
    require,
    reset_config,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "HAS_CURL_CFFI",
    "HAS_SELECTOLAX",
    "ToolkitConfig",
    "ToolkitError",
    "MissingDependencyError",
    "capabilities",
    "configure",
    "describe",
    "get_config",
    "has",
    "optional",
    "public_api",
    "require",
    "reset_config",
    "info_manager",
    "crawler_checks",
    "health_checks",
]

_LAZY = {"info_manager", "crawler_checks", "health_checks"}


def __getattr__(name: str):
    if name in _LAZY:
        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if TYPE_CHECKING:  # pragma: no cover
    from crawler_toolkit import info_manager