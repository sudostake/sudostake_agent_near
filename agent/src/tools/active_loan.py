"""
Active loan tools for the SudoStake agent.

This module exposes two user-triggered actions against a vault contract:

- repay_loan: Owner-only repayment of principal + interest in the requested FT.
- process_claims: Anyone can trigger liquidation after expiry to repay the lender in NEAR.

Both functions:
- attach 1 yoctoNEAR (access control) and budget sufficient gas.
- map common contract panics to user-friendly messages based on the vault contract.
- parse structured EVENT_JSON logs where helpful to provide context.
"""

from typing import Any, Dict, Optional
import os
import json
from logging import Logger
from .context import get_env, get_near, get_logger
from py_near.models import TransactionResult
# Backward-compatible gas/yocto constants: fall back if missing in deployed constants
try:  # pragma: no cover
    from constants import GAS_300_TGAS as _GAS_300_TGAS, YOCTO_1 as _YOCTO_1
except Exception:
    _GAS_300_TGAS = 300_000_000_000_000
    _YOCTO_1 = 1
GAS_300_TGAS: int = _GAS_300_TGAS
YOCTO_1: int = _YOCTO_1
from helpers import (
    run_coroutine,
    get_explorer_url,
    log_contains_event,
    get_failure_message_from_tx_status,
    index_vault_to_firebase,
    # Re-export for process_claims and tests that monkeypatch via tools.active_loan
    format_near_timestamp,
    is_rpc_connectivity_error,
)

# -----------------------------------------------------------------------------
# Constants are imported from constants.py
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# Internal helpers — panic mapping
# -----------------------------------------------------------------------------

def _failure_text(failure: Dict[str, Any]) -> str:
    """Extract the most relevant error text from a contract failure object.

    Prefers the inner FunctionCallError.ExecutionError when present; otherwise
    falls back to a JSON dump for visibility.
    """
    try:
        if isinstance(failure, dict):
            fce = failure.get("FunctionCallError")
            if isinstance(fce, dict):
                exec_err = fce.get("ExecutionError")
                if isinstance(exec_err, str):
                    return exec_err
    except Exception:
        pass
    return json.dumps(failure)

def _map_repay_panic_message(failure: Dict[str, Any], vault_id: str) -> Optional[str]:
    """Return a friendly message for known repay_loan panics or None."""
    s = _failure_text(failure)
    if "Requires attached deposit of exactly 1 yoctoNEAR" in s:
        return (
            "❌ Requires exactly 1 yoctoNEAR attached deposit.\n"
            "This tool attaches it automatically; please retry."
        )
    if "Only the vault owner can repay the loan" in s:
        return (
            "❌ Only the vault owner can repay the loan.\n"
            f"- Vault: `{vault_id}`"
        )
    if "No active loan to repay" in s:
        return (
            "ℹ️ No active loan to repay.\n"
            f"- Vault: `{vault_id}`"
        )
    if "No accepted offer found" in s:
        return (
            "ℹ️ No accepted offer exists.\n"
            f"- Vault: `{vault_id}`\n"
            "- Repayment is only applicable when a lender’s offer was accepted."
        )
    if "Loan has already entered liquidation" in s:
        return (
            "⚠️ Loan is already in liquidation; repay_loan is blocked.\n"
            f"- Vault: `{vault_id}`\n"
            "- Use process_claims to progress repayment in NEAR."
        )
    return None


# process_claims is factored into its own module; see tools/process_claims.py

# -----------------------------------------------------------------------------
# Internal helpers — RPC connectivity hints
# -----------------------------------------------------------------------------

def _rpc_connectivity_hint(ex: Exception, vault_id: str) -> Optional[str]:
    """Return a short, actionable hint if the error looks like an RPC outage.

    Detects common network resolution/connection failures and suggests:
    - Ensuring NEAR_NETWORK matches the vault suffix
    - Checking local network/DNS settings
    """
    if not is_rpc_connectivity_error(ex):
        return None

    # Guess desired network from the vault id
    want_testnet = ".testnet" in vault_id
    suggested_network = "testnet" if want_testnet else "mainnet"
    current_net = os.getenv("NEAR_NETWORK") or "unset"

    return (
        "📡 RPC appears unreachable.\n"
        f"- Current NEAR_NETWORK: `{current_net}` (vault looks like `{suggested_network}`)\n"
        f"- Tip: set `NEAR_NETWORK={suggested_network}` for this vault.\n"
        "- Check your network/DNS and retry shortly."
    )

# -----------------------------------------------------------------------------
# repay_loan
# -----------------------------------------------------------------------------


def repay_loan(vault_id: str) -> None:
    """
    Repay an active SudoStake loan for the given vault.

    This performs the following:
    - Calls `repay_loan` on the vault contract with 1 yoctoNEAR.
    - Checks for contract panics or `repay_loan_failed` events.
    - Indexes the vault to Firebase.
    - Responds with a success message and explorer link if successful.
    """
    
    env = get_env()
    near = get_near()
    logger: Logger = get_logger()
    
    try:
        tx: TransactionResult = run_coroutine(
            near.call(
                contract_id=vault_id,
                method_name="repay_loan",
                args={},
                gas=GAS_300_TGAS,
                amount=YOCTO_1,
            )
        )
        
        # Contract panic?
        failure = get_failure_message_from_tx_status(tx.status)
        if failure:
            mapped = _map_repay_panic_message(failure, vault_id)
            if mapped:
                env.add_reply(mapped)
            else:
                env.add_reply(
                    "❌ Loan repayment failed due to contract panic:\n\n"
                    f"> {json.dumps(failure, indent=2)}"
                )
            return
        
        # Check for log error
        if log_contains_event(tx.logs, "repay_loan_failed"):
            env.add_reply(
                "❌ Loan repayment failed. Funds could not be transferred to the lender."
            )
            return
        
        # Index the updated vault via backend API (best-effort)
        try:
            index_vault_to_firebase(vault_id, tx.transaction.hash)
        except Exception as e:
            logger.warning("Failed to index vault to Firebase: %s", e, exc_info=True)
        
        explorer = get_explorer_url()
        env.add_reply(
            f"✅ **Loan Repaid Successfully**\n"
            f"- 🏦 Vault: [`{vault_id}`]({explorer}/accounts/{vault_id})\n"
            f"- 🔗 Tx: [{tx.transaction.hash}]({explorer}/transactions/{tx.transaction.hash})"
        )
    except Exception as e:
        logger.error("repay_loan failed: %s", e, exc_info=True)
        hint = _rpc_connectivity_hint(e, vault_id)
        extra = f"\n\n{hint}" if hint else ""
        env.add_reply(f"❌ Unexpected error during loan repayment\n\n**Error:** {e}{extra}")


def process_claims(vault_id: str) -> None:
    """Backwards-compatible wrapper around tools.process_claims.process_claims."""
    from .process_claims import process_claims as _impl
    _impl(vault_id)
