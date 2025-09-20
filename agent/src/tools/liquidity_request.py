import json
import asyncio
import requests
import time

from decimal import Decimal, InvalidOperation, DivisionByZero, Overflow
from typing import List, TypedDict, cast, Any, Dict, Literal, Optional, Tuple
from logging import Logger
from datetime import datetime, timezone
from .context import get_env, get_near, get_logger
from token_registry import get_token_metadata, get_token_metadata_by_contract, TokenMeta
from helpers import (
    YOCTO_FACTOR,
    get_factory_contract,
    index_vault_to_firebase,
    run_coroutine, 
    get_explorer_url, 
    log_contains_event,
    get_failure_message_from_tx_status,
    firebase_vaults_api,
    account_id,
    signing_mode,
    format_firestore_timestamp,
)

from py_near.models import TransactionResult

# Define the structure of the liquidity request
class LiquidityRequest(TypedDict):
    token: str
    amount: str
    interest: str
    collateral: str
    duration: int

# Define the structure of an accepted offer
class AcceptedOffer(TypedDict):
    lender: str
    accepted_at: dict[str, object]

# Define the structure of a pending liquidity request
class PendingRequest(TypedDict):
    id: str
    owner: str
    state: str
    liquidity_request: LiquidityRequest

# Define the structure of an active lending request
class ActiveRequest(TypedDict):
    id: str
    owner: str
    state: str
    liquidity_request: LiquidityRequest
    accepted_offer: AcceptedOffer

# Payload schema for accepting a liquidity request via ft_transfer_call
class AcceptLiquidityMsg(TypedDict):
    action: Literal["AcceptLiquidityRequest"]
    token: str
    amount: str
    interest: str
    collateral: str
    duration: int

# -----------------------------------------------------------------------------
# Internal helpers — formatting and time
# -----------------------------------------------------------------------------

def _format_number(value: Decimal, digits: int = 2) -> str:
    """Return a human-friendly number with separators and trimmed decimals."""
    quantum = Decimal(10) ** -digits
    text = f"{value.quantize(quantum):,}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _epoch_seconds_to_utc(secs: int) -> str:
    """Format epoch seconds as 'YYYY-MM-DD HH:MM UTC'."""
    return datetime.fromtimestamp(secs, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _firestore_ts_to_seconds(ts: object) -> Optional[int]:
    """Extract seconds from a Firestore-style timestamp or primitive value."""
    if isinstance(ts, dict) and "_seconds" in ts:
        try:
            return int(ts["_seconds"])  # type: ignore[index]
        except (ValueError, TypeError):
            return None
    if isinstance(ts, (int, str)):
        try:
            return int(ts)
        except (ValueError, TypeError):
            return None
    return None


def _format_time_left(seconds_left: int) -> str:
    """Format seconds as 'Xd Yh Zm'; floors to 0m when <= 0."""
    if seconds_left <= 0:
        return "0m"
    days = seconds_left // 86400
    hours = (seconds_left % 86400) // 3600
    minutes = (seconds_left % 3600) // 60
    parts: List[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


# -----------------------------------------------------------------------------
# Internal helpers — lender positions flow
# -----------------------------------------------------------------------------

def _fetch_lender_positions(factory_contract: str, lender_account_id: str) -> List[ActiveRequest]:
    """Fetch active lending positions for a lender from the web API."""
    api_url = f"{firebase_vaults_api()}/view_lender_positions"
    resp = requests.get(
        api_url,
        params={"factory_id": factory_contract, "lender_id": lender_account_id},
        timeout=10,
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    return cast(List[ActiveRequest], resp.json())


def _enrich_positions(positions: List[ActiveRequest]) -> List[Dict[str, Any]]:
    """Attach timing fields to raw positions for sorting and display."""
    enriched: List[Dict[str, Any]] = []
    now_seconds = int(time.time())
    for pos in positions:
        req = pos.get("liquidity_request")
        acc = pos.get("accepted_offer")
        if not req or not acc:
            continue
        accepted_seconds = _firestore_ts_to_seconds(acc.get("accepted_at"))
        duration_seconds = int(req.get("duration", 0))
        expiry_seconds: Optional[int] = (
            accepted_seconds + duration_seconds if accepted_seconds is not None else None
        )
        seconds_left: Optional[int] = (
            (expiry_seconds - now_seconds) if isinstance(expiry_seconds, int) else None
        )
        enriched.append(
            {
                "raw": pos,
                "accepted_seconds": accepted_seconds,
                "expiry_secs": expiry_seconds,
                "seconds_left": seconds_left,
                "expired": isinstance(seconds_left, int) and seconds_left <= 0,
            }
        )
    return enriched


def _sort_enriched(enriched: List[Dict[str, Any]]) -> None:
    """Sort in-place: expired first, then soonest to expire."""
    def sort_key(e: Dict[str, Any]) -> Tuple[int, int]:
        expired_rank = 0 if e.get("expired") else 1
        expiry_val = e.get("expiry_secs") or 0
        return (expired_rank, int(expiry_val))

    enriched.sort(key=sort_key)


def _format_position_entry(near, explorer_url: str, entry: Dict[str, Any], preloaded_state: Optional[Dict[str, Any]] = None) -> str:
    """Return a formatted block for one position entry, including quick action and liquidation info when eligible."""
    pos = cast(Dict[str, Any], entry["raw"])  # guaranteed present
    req = cast(Dict[str, Any], pos["liquidity_request"])  # guaranteed present
    acc = cast(Dict[str, Any], pos["accepted_offer"])    # guaranteed present

    token_meta = get_token_metadata_by_contract(str(req["token"]))
    decimals = int(token_meta["decimals"])
    symbol = token_meta["symbol"]

    principal = Decimal(str(req["amount"])) / Decimal(10 ** decimals)
    interest = Decimal(str(req["interest"])) / Decimal(10 ** decimals)
    total_due = principal + interest
    collateral_near = Decimal(str(req["collateral"])) / YOCTO_FACTOR
    duration_days = int(req["duration"]) // 86400

    apr_text = "N/A"
    try:
        if principal > 0 and duration_days > 0:
            apr_val = (interest / principal) * Decimal(365) / Decimal(duration_days) * 100
            apr_text = f"{_format_number(apr_val, 2)}%"
    except (InvalidOperation, DivisionByZero, Overflow, ZeroDivisionError):
        apr_text = "N/A"

    acc_ts = acc.get("accepted_at")
    if isinstance(acc_ts, dict):
        accepted_text = format_firestore_timestamp(cast(Dict[str, Any], acc_ts))
    elif isinstance(acc_ts, str):
        accepted_text = format_firestore_timestamp(acc_ts)
    else:
        accepted_text = "Unknown"
    expiry_secs = entry.get("expiry_secs")
    expires_text = _epoch_seconds_to_utc(int(expiry_secs)) if isinstance(expiry_secs, int) else "Unknown"
    seconds_left = entry.get("seconds_left")
    time_left_text = _format_time_left(int(seconds_left)) if isinstance(seconds_left, int) else "Unknown"
    claims_eligible_text = "Yes" if entry.get("expired") else "No"
    action_hint = (
        "Process claims to repay in NEAR." if entry.get("expired") else "Wait; borrower may repay in token."
    )

    # Liquidation status (best-effort) and quick action
    liquidation_block = ""
    if entry.get("expired"):
        try:
            state: Optional[Dict[str, Any]] = preloaded_state
            if state is None:
                state_resp = run_coroutine(near.view(pos['id'], "get_vault_state", {}))
                state = getattr(state_resp, "result", None)
            if isinstance(state, dict):
                liq = state.get("liquidation")
                chain_req = state.get("liquidity_request") or {}
                try:
                    total_collateral_near = Decimal(str(chain_req.get("collateral"))) / YOCTO_FACTOR
                except (InvalidOperation, Overflow, TypeError, ValueError):
                    total_collateral_near = collateral_near
                if liq:
                    liquidated_near = Decimal(str(liq.get("liquidated", "0"))) / YOCTO_FACTOR
                    liquidation_block = (
                        "  • Liquidation: In progress\n"
                        + f"  • Liquidated so far: `{_format_number(liquidated_near, 5)}` NEAR of `{_format_number(total_collateral_near, 5)}` NEAR\n"
                    )
                else:
                    liquidation_block = "  • Liquidation: Not started\n"
            else:
                liquidation_block = "  • Liquidation: Unknown\n"
        except Exception as e:
            # Log the underlying error for diagnostics; present a neutral message to users.
            get_logger().warning(
                "Failed to retrieve liquidation status for %s: %s", pos.get('id'), e, exc_info=True
            )
            liquidation_block = "  • Liquidation: Unknown\n"

    quick_action = (
        f"  • Quick action: `Process claims on {pos['id']}`\n" if entry.get("expired") else ""
    )

    return (
        f"- Vault: [`{pos['id']}`]({explorer_url}/accounts/{pos['id']})\n"
        f"  • Borrower: `{pos.get('owner', 'unknown')}`\n"
        f"  • Token: {symbol} (`{req['token']}`)\n"
        f"  • Principal: `{_format_number(principal)}` {symbol} • Interest: `{_format_number(interest)}` {symbol} • Total: `{_format_number(total_due)}` {symbol}\n"
        f"  • Collateral: `{_format_number(collateral_near)}` NEAR\n"
        f"  • APR: {apr_text}\n"
        f"  • Duration: `{duration_days} days`\n"
        f"  • Accepted: `{accepted_text}` • Expires: `{expires_text}` • Time left: `{time_left_text}`\n"
        f"  • Claims eligible: `{claims_eligible_text}`\n"
        f"  • Action: {action_hint}\n"
        f"{liquidation_block}"
        f"{quick_action}"
        "\n"
    )
    

def request_liquidity(
    vault_id: str,
    amount: int,
    denom: str,
    interest: int,
    duration: int,
    collateral: int,
) -> None:
    """
    Open a SudoStake liquidity request using staked NEAR as collateral.

    Parameters:
    - vault_id (str): Vault account ID (e.g., "vault-0.factory.testnet")
    - amount (int): Requested loan amount
    - denom (str): The requested token denomination (e.g., "usdc")
    - interest (int): Interest in same denomination as amount (e.g., 50)
    - duration (int): Duration in days (e.g., 30)
    - collateral (int): Collateral in NEAR (e.g., 100)
    """
    
    env = get_env()
    near = get_near()
    logger: Logger = get_logger()
    
    # 'headless' or None
    if signing_mode() != "headless":
        env.add_reply(
            "⚠️ No signing keys available. Add `NEAR_ACCOUNT_ID` and "
            "`NEAR_PRIVATE_KEY` to secrets, then try again."
        )
        return
    
    try:
        # Parse amount and resolve token
        token_meta: TokenMeta = get_token_metadata(denom.strip().lower())
        
        # Scale amount using token decimals
        amount_scaled = int((Decimal(amount) * (10 ** token_meta["decimals"])).quantize(Decimal("1")))
        
        # Scale interest using same token decimals
        interest_scaled = int((Decimal(interest) * (10 ** token_meta["decimals"])).quantize(Decimal("1")))
        
        # Convert NEAR collateral to yocto
        collateral_yocto = int((Decimal(collateral) * YOCTO_FACTOR).quantize(Decimal("1")))
        
        # Convert duration to seconds
        duration_secs: int = duration * 86400
        
        # Prepare the transaction arguments 
        args: LiquidityRequest = {
            "token": token_meta["contract"],
            "amount": str(amount_scaled),
            "interest": str(interest_scaled),
            "collateral": str(collateral_yocto),
            "duration": duration_secs,
        }
        
        # Perform the liquidity request call with 1 yoctoNEAR attached
        response: TransactionResult = run_coroutine(
            near.call(
                contract_id=vault_id,
                method_name="request_liquidity",
                args=cast(Dict[str, Any], args),
                gas=300_000_000_000_000,  # 300 TGas
                amount=1,                 # 1 yoctoNEAR deposit
            )
        )
        
        # Catch any panic errors
        failure = get_failure_message_from_tx_status(response.status)
        if failure:
            env.add_reply(
                "❌ Liquidity Request failed with **contract panic**:\n\n"
                f"> {json.dumps(failure, indent=2)}"
            )
            return
        
        # Inspect the logs for event : liquidity_request_failed_insufficient_stake
        if log_contains_event(response.logs, "liquidity_request_failed_insufficient_stake"):
            env.add_reply(
                "❌ Liquidity Request failed\n"
                "> You may not have enough staked NEAR to cover the collateral."
            )
            return
        
        # Index the vault via backend API
        try:
            index_vault_to_firebase(vault_id, response.transaction.hash)
        except Exception as e:
            logger.warning("index_vault_to_firebase failed: %s", e, exc_info=True)
        
        explorer = get_explorer_url()
        env.add_reply(
            f"💧 **Liquidity Request Submitted**\n"
            f"- 🏦 Vault: [`{vault_id}`]({explorer}/accounts/{vault_id})\n"
            f"- 💵 Amount: `{amount}` ({token_meta['symbol']})\n"
            f"- 📈 Interest: `{interest}` {token_meta['symbol']}\n"
            f"- ⏳ Duration: `{duration}` days\n"
            f"- 💰 Collateral: `{collateral}` NEAR\n"
            f"- 🔗 Tx: [{response.transaction.hash}]({explorer}/transactions/{response.transaction.hash})"
        )
        
    except Exception as e:
        env.add_reply(f"❌ Liquidity request failed\n\n**Error:** {e}")


def view_pending_liquidity_requests() -> None:
    """
    Display all pending liquidity requests from the Firebase index
    for vaults minted under the current network's factory contract.
    """
    
    env = get_env()
    logger = get_logger()
    
    try:
        # Resolve factory for the active network
        factory_id = get_factory_contract()
        
        url = f"{firebase_vaults_api()}/view_pending_liquidity_requests"
        response = requests.get(
            url,
            params={"factory_id": factory_id},
            timeout=10,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        
        pending: List[PendingRequest] = response.json()
        
        if not pending:
            env.add_reply("✅ No pending liquidity requests found.")
            return

        message = "**📋 Pending Liquidity Requests**\n\n"
        for item in pending:
            lr = item["liquidity_request"]
            token_meta = get_token_metadata_by_contract(lr["token"])
            decimals = token_meta["decimals"]
            symbol = token_meta["symbol"]
            
            amount = (Decimal(lr["amount"]) / Decimal(10 ** decimals)).quantize(Decimal(1))
            interest = (Decimal(lr["interest"]) / Decimal(10 ** decimals)).quantize(Decimal(1))
            collateral = (Decimal(lr["collateral"]) / YOCTO_FACTOR).quantize(Decimal(1))
            duration_days = lr["duration"] // 86400
            
            message += (
                f"- 🏦 `{item['id']}`\n"
                f"  • Token: `{lr['token']}`\n"
                f"  • Amount: `{amount}` {symbol}\n"
                f"  • Interest: `{interest}` {symbol}\n"
                f"  • Duration: `{duration_days} days`\n"
                f"  • Collateral: `{collateral}` NEAR\n\n"
            )
        
        env.add_reply(message)
            
        
    except Exception as e:
        logger.warning("view_pending_liquidity_requests failed: %s", e, exc_info=True)
        env.add_reply(f"❌ Failed to fetch pending liquidity requests\n\n**Error:** {e}")


def accept_liquidity_request(vault_id: str) -> None:
    """
    Accept a pending liquidity request on the given vault by sending the
    required amount of tokens via `ft_transfer_call`.

    Args:
        vault_id (str): NEAR account ID of the vault (e.g., vault-0.factory.testnet)
    """
    
    env = get_env()
    near = get_near()
    logger: Logger = get_logger()
    
    try:
        response = run_coroutine(near.view(vault_id, "get_vault_state", {}))
        if not response or not hasattr(response, "result") or response.result is None:
            env.add_reply(f"❌ No data returned for `{vault_id}`. Is the contract deployed?")
            return
        
        # Get the result state from the response
        state = response.result
        
        req = state.get("liquidity_request")
        offer = state.get("accepted_offer")
        
        if offer or not req:
            env.add_reply(
                f"❌ `{vault_id}` has no active liquidity request or it has already been accepted."
            )
            return
        # req is present beyond this point
        assert req is not None
        req = cast(LiquidityRequest, req)

        msg_payload: AcceptLiquidityMsg = {
            "action": "AcceptLiquidityRequest",
            "token": req["token"],
            "amount": req["amount"],
            "interest": req["interest"],
            "collateral": req["collateral"],
            "duration": req["duration"],
        }
        
        token_contract = req["token"]
        token_amount = req["amount"]
        
        # Send ft_transfer_call
        tx: TransactionResult = run_coroutine(
            near.call(
                contract_id=token_contract,
                method_name="ft_transfer_call",
                args={
                    "receiver_id": vault_id,
                    "amount": token_amount,
                    "msg": json.dumps(msg_payload),
                },
                gas=300_000_000_000_000,  # 300 TGas
                amount=1,                 # 1 yoctoNEAR deposit
            )
        )
        
        failure = get_failure_message_from_tx_status(tx.status)
        if failure:
            env.add_reply(
                f"❌ Failed to accept liquidity request\n\n> {json.dumps(failure, indent=2)}"
            )
            return
        
        # Index the vault via backend API
        try:
            index_vault_to_firebase(vault_id, tx.transaction.hash)
        except Exception as e:
            logger.warning("index_vault_to_firebase failed: %s", e, exc_info=True)
        
        # Get the token metadata
        token_meta = get_token_metadata_by_contract(token_contract)
        decimals = token_meta["decimals"]
        symbol = token_meta["symbol"]
        token_amount_val = (Decimal(token_amount) / Decimal(10 ** decimals)).quantize(Decimal(1))

        explorer = get_explorer_url()
        env.add_reply(
            f"✅ **Accepted Liquidity Request**\n"
            f"- 🏦 Vault: [`{vault_id}`]({explorer}/accounts/{vault_id})\n"
            f"- 🪙 Token: `{token_contract}`\n"
            f"- 💵 Amount: `{token_amount_val}` {symbol}\n"
            f"- 🔗 Tx: [{tx.transaction.hash}]({explorer}/transactions/{tx.transaction.hash})"
        )
    
    except Exception as e:
        logger.error("accept_liquidity_request failed: %s", e, exc_info=True)
        env.add_reply(f"❌ Error while accepting liquidity request:\n\n**{e}**")


def view_lender_positions() -> None:
    """
    Show all vaults where the current user is the lender with an active loan.

    Uses the web API (sudostake_web_near) for efficiency:
    - GET `${FIREBASE_API}/view_lender_positions?factory_id=...&lender_id=...`
    - Returns a list of entries with `liquidity_request` and `accepted_offer`.
    - Accepts Firestore‐style timestamps for `accepted_at`.
    """

    env = get_env()
    near = get_near()
    logger = get_logger()

    try:
        lender_id = account_id()
        if not lender_id:
            env.add_reply(
                "⚠️ No account ID available. Set `NEAR_ACCOUNT_ID` in secrets, then try again."
            )
            return

        factory_id = get_factory_contract()
        try:
            positions = _fetch_lender_positions(factory_id, lender_id)
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning("view_lender_positions JSON parse failed: %s", e, exc_info=True)
            env.add_reply(
                "❌ Failed to fetch lending positions\n\n"
                f"**Error:** not a JSON response: {e}"
            )
            return
        except Exception as e:
            logger.warning("view_lender_positions API error: %s", e, exc_info=True)
            env.add_reply(
                "❌ Failed to fetch lending positions\n\n"
                f"**Error:** {e}"
            )
            return
        if not positions:
            env.add_reply("✅ You have no active lending positions.")
            return

        enriched = _enrich_positions(positions)
        _sort_enriched(enriched)

        # Prefetch on-chain vault states for expired positions concurrently to avoid N sequential RPCs.
        expired_ids = [cast(Dict[str, Any], e["raw"]).get("id") for e in enriched if e.get("expired")]
        state_by_vault: Dict[str, Optional[Dict[str, Any]]] = {}
        if expired_ids:
            try:
                coros = [near.view(v_id, "get_vault_state", {}) for v_id in expired_ids if isinstance(v_id, str)]
                results = run_coroutine(asyncio.gather(*coros, return_exceptions=True))
                for v_id, res in zip(expired_ids, results):
                    if not isinstance(v_id, str):
                        continue
                    if isinstance(res, Exception):
                        logger.warning("prefetch get_vault_state failed for %s: %s", v_id, res, exc_info=True)
                        state_by_vault[v_id] = None
                        continue
                    st = getattr(res, "result", None)
                    state_by_vault[v_id] = st if isinstance(st, dict) else None
            except Exception as e:
                logger.warning("prefetch gather failed: %s", e, exc_info=True)

        explorer = get_explorer_url()
        blocks: List[str] = [f"**📄 Active Lending Positions for `{lender_id}`**\n"]
        for entry in enriched:
            v_id = cast(Dict[str, Any], entry["raw"]).get("id")
            pre_state = state_by_vault.get(v_id) if isinstance(v_id, str) else None
            blocks.append(_format_position_entry(near, explorer, entry, preloaded_state=pre_state))

        env.add_reply("".join(blocks))

    except Exception as e:
        logger.warning("view_lender_positions failed: %s", e, exc_info=True)
        env.add_reply(f"❌ Failed to fetch lending positions\n\n**Error:** {e}")
