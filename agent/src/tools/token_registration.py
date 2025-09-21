import json
from decimal import Decimal
from typing import Any, Dict, Optional, cast

from logging import Logger

from .context import get_env, get_near, get_logger
from helpers import (
    YOCTO_FACTOR,
    run_coroutine,
    get_explorer_url,
    signing_mode,
    account_id,
    get_failure_message_from_tx_status,
)
from token_registry import get_token_metadata

from py_near.models import TransactionResult
from runtime_constants import GAS_300_TGAS

# Default storage-deposit fallback (~0.00125 NEAR), when token doesn't expose
# storage_balance_bounds or returns an invalid response.
DEFAULT_STORAGE_DEPOSIT_NEAR: Decimal = Decimal("0.00125")
DEFAULT_STORAGE_DEPOSIT_YOCTO: int = int((DEFAULT_STORAGE_DEPOSIT_NEAR * YOCTO_FACTOR).quantize(Decimal("1")))


def _storage_balance_of(token_contract: str, acct: str) -> Optional[Dict[str, Any]]:
    """Return storage balance record for acct, or None if missing/not registered."""
    near = get_near()
    try:
        resp = run_coroutine(near.view(token_contract, "storage_balance_of", {"account_id": acct}))
        if hasattr(resp, "result") and isinstance(resp.result, dict):
            return cast(Dict[str, Any], resp.result)
    except Exception:
        # Non-standard token or view failure
        return None
    return None


def _storage_min_deposit(token_contract: str) -> int:
    """Return minimal required storage deposit (yoctoNEAR). Fallback to DEFAULT_STORAGE_DEPOSIT_YOCTO."""
    near = get_near()
    try:
        resp = run_coroutine(near.view(token_contract, "storage_balance_bounds", {}))
        if hasattr(resp, "result") and isinstance(resp.result, dict):
            min_val = cast(Dict[str, Any], resp.result).get("min")
            if isinstance(min_val, (int, str)):
                return int(min_val)
    except Exception:
        pass
    # Fallback commonly sufficient for NEP-145
    return DEFAULT_STORAGE_DEPOSIT_YOCTO


def register_account_with_token(account: str) -> None:
    """
    Register an account with a NEP-141 token via `storage_deposit`.

    - `account` may be an explicit NEAR account ID (e.g., `vault-0.factory.testnet`).
    - For convenience, accepts special values: `me`, `self`, or empty string, which resolves
      to the current headless account (requires NEAR_ACCOUNT_ID/NEAR_PRIVATE_KEY).
    """
    env = get_env()
    near = get_near()
    logger: Logger = get_logger()

    # Require headless signing like other mutating tools
    if signing_mode() != "headless":
        env.add_reply(
            "⚠️ No signing keys available. Add `NEAR_ACCOUNT_ID` and `NEAR_PRIVATE_KEY` to secrets, then try again."
        )
        return

    # Resolve special values to the current account
    acct = account.strip() if isinstance(account, str) else ""
    if acct in ("", "me", "self"):
        acct = account_id() or ""
    if not acct:
        env.add_reply("⚠️ No account ID available. Set `NEAR_ACCOUNT_ID` in secrets, then try again.")
        return

    try:
        # Resolve the canonical token contract for this network (default: USDC)
        token_meta = get_token_metadata("usdc")
        token_contract = token_meta["contract"]

        # Short-circuit when already registered
        bal = _storage_balance_of(token_contract, acct)
        if isinstance(bal, dict):
            env.add_reply(
                f"✅ `{acct}` is already registered with `{token_contract}`."
            )
            return

        deposit = _storage_min_deposit(token_contract)
        tx: TransactionResult = run_coroutine(
            near.call(
                contract_id=token_contract,
                method_name="storage_deposit",
                args={"account_id": acct, "registration_only": True},
                gas=GAS_300_TGAS,
                amount=deposit,
            )
        )

        failure = get_failure_message_from_tx_status(tx.status)
        if failure:
            env.add_reply(
                "❌ Failed to register account with token\n\n" + f"> {json.dumps(failure, indent=2)}"
            )
            return

        explorer = get_explorer_url()
        env.add_reply(
            f"✅ **Registered Account With Token**\n"
            f"- 👤 Account: `{acct}`\n"
            f"- 🪙 Token: `{token_contract}`\n"
            f"- 🔗 Tx: [{tx.transaction.hash}]({explorer}/transactions/{tx.transaction.hash})"
        )

    except Exception as e:
        logger.warning("register_account_with_token failed: %s", e, exc_info=True)
        env.add_reply(f"❌ Failed to register account with token\n\n**Error:** {e}")


# Backwards-compatible wrappers (not registered as tools)
def register_vault_with_token(vault_id: str, token_contract: str) -> None:
    """
    Register the given vault account with the NEP-141 token contract.

    Notes:
    - `token_contract` parameter is accepted for compatibility but ignored.
    - The actual token contract is resolved from TOKEN_REGISTRY for the
      current `NEAR_NETWORK` (defaults to USDC per network).
    """
    # token_contract param ignored; we resolve from registry
    register_account_with_token(vault_id)


def register_me_with_token(token_contract: str) -> None:
    """
    Register the current headless account with the NEP-141 token contract.

    Notes:
    - `token_contract` parameter is accepted for compatibility but ignored.
    - The actual token contract is resolved from TOKEN_REGISTRY for the
      current `NEAR_NETWORK` (defaults to USDC per network).
    """
    # token_contract param ignored; we resolve from registry
    register_account_with_token("me")
