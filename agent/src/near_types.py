from typing import Protocol, Awaitable, Any, Dict

# Keep this module dependency-free to avoid circular imports.

class NearClient(Protocol):
    """Minimal protocol for the NEAR client used by tools/helpers.

    Matches the subset of methods our code calls, regardless of the concrete
    implementation (py-near Account, wrapper, or a mock in tests).
    """

    def call(
        self,
        *,
        contract_id: str,
        method_name: str,
        args: Dict[str, Any],
        gas: int,
        amount: int,
    ) -> Awaitable[Any]:
        ...

    def view(self, contract_id: str, method_name: str, args: Dict[str, Any]) -> Awaitable[Any]:
        ...

    def send_money(self, *, account_id: str, amount: int) -> Awaitable[Any]:
        ...

    def get_balance(self) -> Awaitable[int]:
        ...

__all__ = ["NearClient"]

