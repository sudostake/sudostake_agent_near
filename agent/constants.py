"""Runtime constants used by the agent (package root).

Note: There are two constants modules:
- `agent/constants.py` (this file) used when the package root is on sys.path
- `agent/src/constants.py` used by tests that add `agent/src` to sys.path

Both define the same values to avoid import errors across environments.
"""

NANOSECONDS_PER_SECOND: int = 1_000_000_000  # 1 s = 1 000 000 000 ns
