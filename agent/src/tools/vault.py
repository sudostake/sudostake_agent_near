"""Vault-related helpers: formatting, state views, and user vaults."""

import requests
import json as _json
import textwrap
import time

from decimal import Decimal
from typing import List, cast
from datetime import timedelta
from logging import Logger
from .context import get_env, get_near, get_logger
from runtime_constants import NANOSECONDS_PER_SECOND, GAS_300_TGAS, YOCTO_1
from helpers import (
    get_factory_contract,
    USDC_FACTOR,
    YOCTO_FACTOR,
    firebase_vaults_api,
    signing_mode,
    account_id,
    run_coroutine,
    format_near_timestamp,
    get_failure_message_from_tx_status,
    get_explorer_url,
    log_contains_event,
    find_event_data,
    index_vault_to_firebase,
)


# Friendly panic → reply mappings for ownership transfer
OWNERSHIP_TRANSFER_PANIC_MAP: dict[str, str] = {
    "Requires attached deposit of exactly 1 yoctoNEAR": (
        "❌ Requires exactly 1 yoctoNEAR attached deposit.\n"
        "This tool attaches it automatically; please retry."
    ),
    "Only the vault owner can transfer ownership": (
        "❌ Only the current vault owner may transfer ownership."
    ),
    "New owner must be different from the current owner": (
        "❌ New owner must be different from the current owner."
    ),
}

def format_duration(seconds: int) -> str:
    """Convert a duration in seconds to a human-readable string."""
    delta = timedelta(seconds=seconds)
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    
    parts: List[str] = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    return " ".join(parts) or "0m"

def format_remaining(seconds_left: int) -> str:
    """
    Turn a positive number of seconds into “Xd Yh Zm”.
    Always shows at least minutes; caps at 0m when expired.
    """
    if seconds_left <= 0:
        return "0m"
    return format_duration(seconds_left)


def show_help_menu() -> None:
    """
    Display a list of supported commands the agent can respond to.
    This is shown when the user types `help`.
    """
    
    help_text = textwrap.dedent("""
        **SudoStake Agent Commands**
        
        __Main Account__
        • what's my main account balance?

        __Vaults__
        • mint vault  
        • view state for <vault>  
        • view available balance in <vault>  
        • transfer <amount> to <vault>  
        • withdraw <amount> from <vault>  
        • withdraw <amount> from <vault> to <receiver>  
        • transfer ownership of <vault> to <new_owner>  
        • show my vaults  

        __Staking__
        • delegate <amount> to <validator> from <vault>  
        • undelegate <amount> from <validator> for <vault>  
        • claim unstaked balance from <validator> for <vault>  
        • show delegation summary for <vault>  
        • show <vault> delegation status with <validator>  
        
        __Liquidity Request__
        • Open a liquidity request on <vault> for <amount> USDC, <amount> USDC interest, <n>-day term, <amount> NEAR collateral  
        • Show me all pending liquidity requests  
        • Accept liquidity request opened by <vault>  
        • Show me all my active lending positions  
        
        __Token Registration__
        • Register <account> with token  
          (defaults to the network's USDC token if not specified)  
        
        __Active Loan__
        • Repay loan on <vault>  
        • Process claims on <vault>  
        
        __SudoStake Docs__
        • Query SudoStake Docs  

        _You can type any of these in plain English to get started._
    """)

    get_env().add_reply(help_text.strip())


def vault_state(vault_id: str) -> None:
    """
    Fetch the on-chain state for `vault_id` and send it to the user.

    Args:
      vault_id: NEAR account ID of the vault.
    """
    
    env = get_env()
    near = get_near()
    logger: Logger = get_logger()

    try:
        response = run_coroutine(near.view(vault_id, "get_vault_state", {}))
        result_obj = getattr(response, "result", None)
        if result_obj is None:
            env.add_reply(f"❌ No data returned for `{vault_id}`. Is the contract deployed?")
            return
        
        # Get the result state from the response
        state = result_obj
        
        # Add vault state summary
        env.add_reply(
            f"✅ **Vault State: `{vault_id}`**\n\n"
            f"| Field                  | Value                       |\n"
            f"|------------------------|-----------------------------|\n"
            f"| Owner                  | `{state['owner']}`          |\n"
            f"| Index                  | `{state['index']}`          |\n"
            f"| Version                | `{state['version']}`        |\n"
            f"| Listed for Takeover    | `{state['is_listed_for_takeover']}` |\n"
            f"| Active Request         | `{state['liquidity_request'] is not None}` |\n"
            f"| Accepted Offer         | `{state['accepted_offer'] is not None}` |\n"
        )
        
        # Add liquidity request summary if present
        if state.get("liquidity_request"):
            req = state["liquidity_request"]
            usdc_amount = Decimal(req["amount"]) / USDC_FACTOR
            usdc_interest = Decimal(req["interest"]) / USDC_FACTOR
            near_collateral = Decimal(req["collateral"]) / YOCTO_FACTOR
            duration = format_duration(int(req["duration"]))
            created_at = format_near_timestamp(int(req["created_at"]))
            
            env.add_reply(
                "**📦 Liquidity Request Summary**\n\n"
                "| Field        | Value                   |\n"
                "|--------------|-------------------------|\n"
                f"| Token       | `{req['token']}`        |\n"
                f"| Amount      | **{usdc_amount:.2f} USDC** |\n"
                f"| Interest    | **{usdc_interest:.2f} USDC** |\n"
                f"| Collateral  | **{near_collateral:.5f} NEAR** |\n"
                f"| Duration    | `{duration}`            |\n"
                f"| Created At  | `{created_at}`          |"
            )
        
        # Add accepted offer summary if present
        accepted = state.get("accepted_offer")
        if accepted:
            lender       = accepted["lender"]
            accepted_ns  = int(accepted["accepted_at"])
            accepted_at  = format_near_timestamp(accepted_ns)
            
            req = state.get("liquidity_request")
            expiry_row = "| Status        | `Expired`           |\n"
            
            if req and "duration" in req:
                duration_s  = int(req["duration"])
                expiry_ns   = accepted_ns + duration_s * NANOSECONDS_PER_SECOND
                secs_left   = int(expiry_ns / NANOSECONDS_PER_SECOND) - int(time.time())
                if secs_left > 0:
                    expiry_row = (
                        f"| Expiring In   | `{format_remaining(secs_left)}` |\n"
                    )
            
            env.add_reply(
                "**🤝 Accepted Offer Summary**\n\n"
                "| Field        | Value              |\n"
                "|--------------|--------------------|\n"
                f"| Lender      | `{lender}`         |\n"
                f"| Accepted At | `{accepted_at}`    |\n"
                f"{expiry_row}"
            )
        
        # Add liquidation summary if present
        if state.get("liquidation") and state.get("liquidity_request"):
            req = state["liquidity_request"]
            total_debt = Decimal(req["collateral"]) / YOCTO_FACTOR
            liquidated = Decimal(state["liquidation"]["liquidated"]) / YOCTO_FACTOR
            remaining = total_debt - liquidated
            
            env.add_reply(
                "**⚠️ Liquidation Summary**\n\n"
                "| Field             | Amount                    |\n"
                "|-------------------|---------------------------|\n"
                f"| Total Debt       | **{total_debt:.5f} NEAR** |\n"
                f"| Liquidated       | **{liquidated:.5f} NEAR** |\n"
                f"| Outstanding Debt | **{remaining:.5f} NEAR**  |"
            )
            
    except Exception as e:
        logger.error("vault_state RPC error for %s: %s", vault_id, e, exc_info=True)
        env.add_reply(f"❌ Failed to fetch vault state for `{vault_id}`\n\n**Error:** {e}")


def view_user_vaults() -> None:
    """
    List every SudoStake vault owned by the *current* head-less signer.

    • Requires `NEAR_ACCOUNT_ID` + `NEAR_PRIVATE_KEY` in secrets  
    • Uses `$NEAR_NETWORK` to resolve the factory contract  
    • Calls the Firebase Cloud Function:  get_user_vaults
    """
    
    env = get_env()
    log: Logger = get_logger()
    
    # 'headless' or None
    if signing_mode() != "headless":
        env.add_reply(
            "⚠️ No signing keys available. Add `NEAR_ACCOUNT_ID` and "
            "`NEAR_PRIVATE_KEY` to secrets, then try again."
        )
        return
    
    # Get the signer's account id
    acct_id = account_id()
    
    # Resolve factory for the active network
    factory_id = get_factory_contract()
    
    # Call the Firebase Cloud Function to get vaults
    url = (
        f"{firebase_vaults_api()}/get_user_vaults"
        f"?owner={acct_id}&factory_id={factory_id}"
    )
    
    try:
        resp    = requests.get(url, timeout=10)
        resp.raise_for_status()
        vaults: List[str] = cast(List[str], resp.json())
        
        if not vaults:
            env.add_reply(f"🔍 No vaults found for `{acct_id}`")
            return
        
        count  = len(vaults)
        plural = "" if count == 1 else "s"
        lines  = "\n".join(f"- {v}" for v in vaults)
        
        env.add_reply(
            f"**You have {count} vault{plural} in total**\n{lines}"
        )
    
    except Exception as e:
        log.error("view_user_vaults error for %s: %s", acct_id, e, exc_info=True)
        env.add_reply(f"❌ Failed to fetch vault list\n\n**Error:** {e}")


def transfer_ownership(vault_id: str, new_owner: str) -> None:
    """
    Transfer ownership of `vault_id` to `new_owner`.

    • Requires headless signing (NEAR_ACCOUNT_ID + NEAR_PRIVATE_KEY).
    • Attaches exactly 1 yoctoNEAR per contract access control.
    • Maps common panics to user-friendly messages.
    • Surfaces EVENT_JSON ownership_transferred details when available.
    """

    env = get_env()
    near = get_near()
    logger: Logger = get_logger()

    # Require headless signing for state changes
    if signing_mode() != "headless":
        env.add_reply(
            "⚠️ I can't sign transactions in this session.\n"
            "Add `NEAR_ACCOUNT_ID` and `NEAR_PRIVATE_KEY` to your run's "
            "secrets, then try again."
        )
        return

    # Basic input check
    if not new_owner or not isinstance(new_owner, str):
        env.add_reply("❌ Please provide a valid `new_owner` account ID.")
        return

    try:
        tx = run_coroutine(
            near.call(
                contract_id=vault_id,
                method_name="transfer_ownership",
                args={"new_owner": new_owner},
                gas=GAS_300_TGAS,
                amount=YOCTO_1,          # 1 yoctoNEAR
            )
        )

        failure = get_failure_message_from_tx_status(tx.status)
        if failure:
            s = str(failure)
            # Friendly mappings based on contract messages
            for pattern, reply in OWNERSHIP_TRANSFER_PANIC_MAP.items():
                if pattern in s:
                    env.add_reply(reply)
                    return

            # Generic fallback
            env.add_reply(
                "❌ Ownership transfer failed due to contract panic:\n\n"
                f"> {_json.dumps(failure, indent=2)}"
            )
            return

        explorer = get_explorer_url()

        # Extract details from logs if present
        old_owner_str = None
        new_owner_str = None
        if log_contains_event(tx.logs, "ownership_transferred"):
            data = find_event_data(tx.logs, "ownership_transferred") or {}
            old_owner_str = data.get("old_owner")
            new_owner_str = data.get("new_owner")

        details = []
        if old_owner_str or new_owner_str:
            if old_owner_str:
                details.append(f"Old owner: `{old_owner_str}`")
            if new_owner_str:
                details.append(f"New owner: `{new_owner_str}`")
        detail_text = ("\n- " + "\n- ".join(details)) if details else ""

        # Best-effort: index updated vault to backend
        try:
            index_vault_to_firebase(vault_id, tx.transaction.hash)
        except Exception as e:
            logger.warning("index_vault_to_firebase failed: %s", e, exc_info=True)

        env.add_reply(
            f"✅ **Ownership Transferred**\n"
            f"- Vault: [`{vault_id}`]({explorer}/accounts/{vault_id})\n"
            f"- 🔗 Tx: [{tx.transaction.hash}]({explorer}/transactions/{tx.transaction.hash})"
            f"{detail_text}"
        )

    except Exception as e:
        logger.error("transfer_ownership error: %s", e, exc_info=True)
        env.add_reply(
            f"❌ Failed to transfer ownership of `{vault_id}` to `{new_owner}`\n\n**Error:** {e}"
        )
