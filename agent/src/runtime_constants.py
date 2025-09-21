"""
Runtime constants wrapper:

Imports values from the deployed `constants` module when available. If a
value is missing (older registry builds), provides a safe default so tools
can import a single source without scattering try/excepts.
"""

from typing import Any

try:
    import constants as _CONST  # type: ignore[import-not-found]
except Exception:
    _CONST = None  # type: ignore[assignment]


def _get(name: str, default: Any) -> Any:
    if _CONST is not None and hasattr(_CONST, name):
        return getattr(_CONST, name)
    return default


# Defaults used when not provided by deployed constants
GAS_300_TGAS: int = int(_get("GAS_300_TGAS", 300_000_000_000_000))
YOCTO_1: int = int(_get("YOCTO_1", 1))
NANOSECONDS_PER_SECOND: int = int(_get("NANOSECONDS_PER_SECOND", 1_000_000_000))

