"""Runtime constants used by the agent tools.

This mirrors values used in tests under `agent/constants.py` so that
modules imported with `agent/src` on `sys.path` can resolve `constants`.
"""

# 1 second = 1_000_000_000 nanoseconds
NANOSECONDS_PER_SECOND: int = 1_000_000_000

# Default gas budget for state-changing calls that drive callbacks.
GAS_300_TGAS: int = 300_000_000_000_000

# Convenience constant for attaching exactly one yoctoNEAR to payable methods.
YOCTO_1: int = 1
