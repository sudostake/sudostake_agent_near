import logging

from nearai.agents.environment import Environment
from py_near.models import TransactionResult
from logging import Logger


# Globals (initialized via set_context before use)
from typing import Optional, Protocol, Awaitable, Any, Dict


class NearClient(Protocol):
    """Minimal protocol for the NEAR client used by tools.

    Matches the subset of methods our code calls, regardless of the concrete
    implementation (py-near Account, wrapper, or mock in tests).
    """

    def call(
        self,
        *,
        contract_id: str,
        method_name: str,
        args: Dict[str, Any],
        gas: int,
        amount: int,
    ) -> Awaitable[TransactionResult]:
        ...

    def view(self, contract_id: str, method_name: str, args: Dict[str, Any]) -> Awaitable[Any]:
        ...

    def send_money(self, *, account_id: str, amount: int) -> Awaitable[TransactionResult]:
        ...

    def get_balance(self) -> Awaitable[int]:
        ...


_env: Optional[Environment] = None
_near: Optional[NearClient] = None

# Logger for this module
_logger = logging.getLogger(__name__)

# ensure logs show up if enabled by the host application


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
    return _logger
