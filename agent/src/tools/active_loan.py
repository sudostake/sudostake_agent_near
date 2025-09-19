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
import re
from logging import Logger
from decimal import Decimal
from .context import get_env, get_near, get_logger
from py_near.models import TransactionResult
from helpers import (
    run_coroutine,
    get_explorer_url,
    log_contains_event,
    get_failure_message_from_tx_status,
    index_vault_to_firebase,
    format_near_timestamp,
    signing_mode,
    find_event_data,
    YOCTO_FACTOR,
)

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

GAS_300_TGAS: int = 300_000_000_000_000
YOCTO_1: int = 1


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


def _map_process_claims_panic_message(
    failure: Dict[str, Any], vault_id: str
) -> Optional[str]:
    """Return a friendly message for known process_claims panics or None."""
    try:
        s = _failure_text(failure)

        # Not expired yet
        m = re.search(r"Liquidation not allowed until (\d+)", s)
        if m:
            ts_ns = int(m.group(1))
            when = format_near_timestamp(ts_ns)
            return (
                "⏳ Liquidation not allowed yet.\n"
                f"- Earliest at: `{when}`\n"
                f"- Vault: `{vault_id}`\n"
                "- Tip: Run this again after the deadline."
            )

        # No accepted offer
        if "No accepted offer found" in s:
            return (
                "ℹ️ No active loan to liquidate.\n"
                f"- Vault: `{vault_id}`\n"
                "- There is no accepted offer; liquidation is not applicable."
            )

        # Processing lock busy
        # Matches: Vault busy with "ProcessKind"  OR  Vault busy with ProcessKind
        m2 = re.search(r"Vault\s+busy\s+with\s+\"?([A-Za-z]+)\"?", s)
        if m2:
            kind = m2.group(1)
            return (
                "⏳ Vault is busy processing another step.\n"
                f"- Operation: `{kind}`\n"
                f"- Vault: `{vault_id}`\n"
                "- Tip: Wait for callbacks to finish, then try again."
            )

        # Missing 1 yocto
        if "Requires attached deposit of exactly 1 yoctoNEAR" in s:
            return (
                "❌ Requires exactly 1 yoctoNEAR attached deposit.\n"
                "This tool attaches it automatically; please retry."
            )
    except Exception:
        # Fall through to generic handling at call site
        return None

    return None


# -----------------------------------------------------------------------------
# Internal helpers — shared
# -----------------------------------------------------------------------------

def _index_vault_best_effort(logger: Logger, vault_id: str, tx_hash: str) -> None:
    """Attempt to index the vault; log a warning on failure without raising."""
    try:
        index_vault_to_firebase(vault_id, tx_hash)
    except Exception as e:
        logger.warning("Failed to index vault to Firebase: %s", e, exc_info=True)


# -----------------------------------------------------------------------------
# Internal helpers — RPC connectivity hints
# -----------------------------------------------------------------------------

def _rpc_connectivity_hint(ex: Exception, vault_id: str) -> Optional[str]:
    """Return a short, actionable hint if the error looks like an RPC outage.

    Detects common network resolution/connection failures and suggests:
    - Ensuring NEAR_NETWORK matches the vault suffix
    - Checking local network/DNS settings
    """
    s = str(ex)
    indicators = (
        "RPC not available",
        "nodename nor servname",
        "Name or service not known",
        "getaddrinfo",
        "Failed to establish a new connection",
        "Max retries exceeded",
        "Temporary failure in name resolution",
    )
    if not any(x in s for x in indicators):
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
        
        # Index the updated vault via backend API
        _index_vault_best_effort(logger, vault_id, tx.transaction.hash)
        
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
    """
    Process claims for the given vault.

    Behavior:
    - Calls `process_claims` on the vault with 1 yoctoNEAR.
    - Parses logs for liquidation lifecycle events and surfaces actionable guidance.
    - Handles contract panics (e.g., not yet overdue, lock busy) with friendly messaging.
    - Indexes the vault to Firebase for fresh UI state.

    Args:
      vault_id: NEAR account ID of the target vault (e.g., "vault-0.factory.testnet").
    """
    env = get_env()
    
    # Require headless signing to send a state-changing tx
    if signing_mode() != "headless":
        env.add_reply(
            "⚠️ No signing keys available. Add `NEAR_ACCOUNT_ID` and `NEAR_PRIVATE_KEY` "
            "to secrets, then try again."
        )
        return

    near = get_near()
    logger: Logger = get_logger()

    try:
        tx: TransactionResult = run_coroutine(
            near.call(
                contract_id=vault_id,
                method_name="process_claims",
                args={},
                gas=GAS_300_TGAS,  # drive callbacks
                amount=YOCTO_1,    # 1 yoctoNEAR
            )
        )

        # Contract panic? Provide helpful mapping when possible.
        failure = get_failure_message_from_tx_status(tx.status)
        if failure:
            mapped = _map_process_claims_panic_message(failure, vault_id)
            if mapped:
                env.add_reply(mapped)
            else:
                env.add_reply(
                    "❌ Processing claims failed due to contract panic:\n\n"
                    f"> {json.dumps(failure, indent=2)}"
                )
            return

        # Index the updated vault via backend API (best-effort)
        _index_vault_best_effort(logger, vault_id, tx.transaction.hash)

        explorer = get_explorer_url()

        # Interpret logs for user-facing status
        started = log_contains_event(tx.logs, "liquidation_started")
        completed = log_contains_event(tx.logs, "liquidation_complete")
        unstake_added = log_contains_event(tx.logs, "unstake_recorded")
        waiting = log_contains_event(tx.logs, "liquidation_progress")
        unstake_failed = log_contains_event(tx.logs, "unstake_failed")

        if completed:
            # Try to surface total repaid from event payload
            completion_data = find_event_data(tx.logs, "liquidation_complete")
            total_repaid = (completion_data or {}).get("total_repaid")
            extra = ""
            if total_repaid:
                # Always include raw yoctoNEAR to preserve precision and tests
                extra = f"\n- 💰 Total repaid: `{total_repaid}` yoctoNEAR"
                # Append human-readable NEAR approximation
                try:
                    near_amt = (Decimal(total_repaid) / YOCTO_FACTOR).quantize(Decimal("0.000001"))
                    extra += f" (~{near_amt} NEAR)"
                except Exception:
                    pass
            env.add_reply(
                f"✅ **Liquidation Complete** — lender fully repaid.{extra}\n"
                f"- 🏦 Vault: [`{vault_id}`]({explorer}/accounts/{vault_id})\n"
                f"- 🔗 Tx: [{tx.transaction.hash}]({explorer}/transactions/{tx.transaction.hash})"
            )
            return

        # Partial progress path
        progress_lines: list[str] = []
        if started:
            started_data = find_event_data(tx.logs, "liquidation_started")
            started_when = None
            try:
                at_value = (started_data or {}).get("at") if started_data else None
                if at_value is not None:
                    at_ns = int(at_value)
                    if at_ns > 0:
                        started_when = format_near_timestamp(at_ns)
            except Exception:
                started_when = None
            lender = (started_data or {}).get("lender") if started_data else None
            line = "• Liquidation started."
            if lender:
                line += f" Lender: `{lender}`."
            if started_when:
                line += f" At: `{started_when}`."
            progress_lines.append(line)
        if unstake_added:
            progress_lines.append(
                "• Unstake recorded — wait ~4 epochs for NEAR to mature."
            )
        if waiting:
            progress_lines.append(
                "• Waiting for available/matured NEAR; re-run to continue."
            )
        if unstake_failed:
            failed = find_event_data(tx.logs, "unstake_failed") or {}
            v = failed.get("validator")
            amt = failed.get("amount")
            msg = "• Warning: an unstake attempt failed on a validator."
            if v:
                msg += f" Validator: `{v}`."
            if amt:
                # Always show raw yoctoNEAR and append readable NEAR when possible
                amount_phrase = f" Amount: `{amt}` yoctoNEAR"
                try:
                    near_amt = (Decimal(amt) / YOCTO_FACTOR).quantize(Decimal("0.000001"))
                    amount_phrase += f" (~{near_amt} NEAR)"
                except Exception:
                    pass
                msg += amount_phrase + "."
            progress_lines.append(msg)

        # Attach granular details when available
        if waiting:
            data = find_event_data(tx.logs, "liquidation_progress")
            reason = (data or {}).get("reason")
            if isinstance(reason, str) and reason:
                progress_lines.append(f"• Reason: {reason}.")
        if unstake_added:
            data = find_event_data(tx.logs, "unstake_recorded")
            if data:
                validator = data.get("validator")
                amount = data.get("amount")
                epoch = data.get("epoch_height")
                detail = "• Unstake recorded"
                if validator:
                    detail += f" on `{validator}`"
                if amount:
                    detail += f" amount `{amount}` yoctoNEAR"
                    try:
                        near_amt = (Decimal(amount) / YOCTO_FACTOR).quantize(Decimal("0.000001"))
                        detail += f" (~{near_amt} NEAR)"
                    except Exception:
                        pass
                if epoch:
                    detail += f" at epoch `{epoch}`"
                progress_lines.append(detail + ".")

        if progress_lines:
            env.add_reply(
                "🔄 **Claims Processing In Progress**\n"
                f"- 🏦 Vault: [`{vault_id}`]({explorer}/accounts/{vault_id})\n"
                f"- 🔗 Tx: [{tx.transaction.hash}]({explorer}/transactions/{tx.transaction.hash})\n"
                + "\n".join(progress_lines)
            )
            return

        # Fallback generic success
        env.add_reply(
            f"✅ Processed claims step.\n"
            f"- 🏦 Vault: [`{vault_id}`]({explorer}/accounts/{vault_id})\n"
            f"- 🔗 Tx: [{tx.transaction.hash}]({explorer}/transactions/{tx.transaction.hash})\n"
            "- If not fully repaid, run again as more NEAR matures."
        )

    except Exception as e:
        logger.error("process_claims failed: %s", e, exc_info=True)
        hint = _rpc_connectivity_hint(e, vault_id)
        extra = f"\n\n{hint}" if hint else ""
        env.add_reply(f"❌ Unexpected error during claims processing\n\n**Error:** {e}{extra}")
