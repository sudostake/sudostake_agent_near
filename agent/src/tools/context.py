import logging
import os
import sys

from nearai.agents.environment import Environment
from logging import Logger
from typing import Optional
from near_types import NearClient

_env: Optional[Environment] = None
_near: Optional[NearClient] = None

_logger = logging.getLogger(__name__)

def _ensure_console_logging() -> None:
    """Attach a console handler to this module logger if none exists.

    Respects `SUDOSTAKE_LOG_LEVEL` (default INFO). Prevents duplicate logs by
    only adding a handler when the logger has none, and disables propagation so
    lines appear exactly once in the console.
    """

    level_name = os.getenv("SUDOSTAKE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    _logger.setLevel(level)
    if not _logger.handlers:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        _logger.addHandler(handler)
        # Avoid double-printing if root logger is configured elsewhere
        _logger.propagate = False

# Ensure console logging is active on import
_ensure_console_logging()


def set_context(env: Environment, near: NearClient) -> None:
    global _env, _near
    _env = env
    _near = near


def get_env() -> Environment:
    if _env is None:
        raise RuntimeError("Environment context not initialized")
    return _env


def get_near() -> NearClient:
    if _near is None:
        raise RuntimeError("NEAR context not initialized")
    return _near


def get_logger() -> Logger:
    # Ensure logger stays configured even if something clears handlers
    _ensure_console_logging()
    return _logger
