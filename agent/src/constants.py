"""Runtime constants used by the agent tools.

This mirrors values used in tests under `agent/constants.py` so that
modules imported with `agent/src` on `sys.path` can resolve `constants`.
"""

# 1 second = 1_000_000_000 nanoseconds
NANOSECONDS_PER_SECOND: int = 1_000_000_000

