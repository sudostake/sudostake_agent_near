import json
import asyncio
import requests
import time
import re

from decimal import Decimal, InvalidOperation, DivisionByZero, Overflow
from typing import List, TypedDict, cast, Any, Dict, Literal, Optional, Tuple
from logging import Logger
from datetime import datetime, timezone
from .context import get_env, get_near, get_logger
from token_registry import get_token_metadata, get_token_metadata_by_contract, TokenMeta
import helpers
from helpers import (
    YOCTO_FACTOR,
    get_factory_contract,
    run_coroutine, 
    get_explorer_url, 
    log_contains_event,
    find_event_data,
    get_failure_message_from_tx_status,
    firebase_vaults_api,
    account_id,
    signing_mode,
    format_firestore_timestamp,
    format_near_timestamp,
)

# Re-export for tests that monkeypatch tools.liquidity_request.index_vault_to_firebase
index_vault_to_firebase = helpers.index_vault_to_firebase  # type: ignore[assignment]

from py_near.models import TransactionResult
from constants import GAS_300_TGAS, YOCTO_1

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
        # Be strict about accepted types to satisfy type checkers
        raw = ts["_seconds"]  # type: ignore[index]
        if isinstance(raw, (int, str)):
            try:
                return int(raw)
            except (ValueError, TypeError):
                return None
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
        dur_raw = cast(Dict[str, Any], req).get("duration")
        duration_seconds = int(dur_raw) if isinstance(dur_raw, (int, str)) else 0
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
        expiry_val = e.get("expiry_secs")
        expiry_int = int(expiry_val) if isinstance(expiry_val, (int, str)) else 0
        return (expired_rank, expiry_int)

    enriched.sort(key=sort_key)


def _format_position_entry(explorer_url: str, entry: Dict[str, Any], preloaded_state: Optional[Dict[str, Any]] = None) -> str:
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
    dur_val = req.get("duration") if isinstance(req, dict) else None
    duration_days = (int(dur_val) // 86400) if isinstance(dur_val, (int, str)) else 0

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
    expires_text = _epoch_seconds_to_utc(expiry_secs) if isinstance(expiry_secs, int) else "Unknown"
    seconds_left = entry.get("seconds_left")
    time_left_text = _format_time_left(seconds_left) if isinstance(seconds_left, int) else "Unknown"
    claims_eligible_text = "Yes" if entry.get("expired") else "No"
    action_hint = (
        "Process claims to repay in NEAR." if entry.get("expired") else "Wait; borrower may repay in token."
    )

    # Liquidation status (best-effort) and quick action
    liquidation_block = ""
    if entry.get("expired"):
        state = preloaded_state
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

    quick_action = (
        f"  • Quick action: `Process claims on {pos['id']}`\n" if entry.get("expired") else ""
    )

    # Assemble and return the formatted block
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


# -----------------------------------------------------------------------------
# Internal helpers — request_liquidity panic mapping
# -----------------------------------------------------------------------------

_RE_VAULT_BUSY = re.compile(r"Vault\s+busy\s+with\s+\"?([A-Za-z]+)\"?")


def _failure_text(failure: Dict[str, Any]) -> str:
    """Extract the most relevant error string from a failure dict."""
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


def _map_request_liquidity_panic_message(failure: Dict[str, Any]) -> Optional[str]:
    """Return a user-friendly mapping for common request_liquidity panics."""
    s = _failure_text(failure)

    # Access control
    if "Requires attached deposit of exactly 1 yoctoNEAR" in s:
        return (
            "Reason: Requires exactly 1 yoctoNEAR attached deposit.\n"
            "Tip: This tool attaches it automatically; please retry."
        )

    # Owner-only
    if "Only the vault owner can request liquidity" in s:
        return "Reason: Only the vault owner can request liquidity."

    # State preconditions
    known_reasons = (
        "A liquidity request is already in progress",
        "A request is already open",
        "Vault is already matched with a lender",
        "Counter-offers must be cleared",
        "Collateral must be positive",
        "Requested amount must be greater than zero",
        "Duration must be non-zero",
    )
    for r in known_reasons:
        if r in s:
            return f"Reason: {r}."

    # Processing lock
    m = _RE_VAULT_BUSY.search(s)
    if m:
        return f"Reason: Vault busy with `{m.group(1)}`. Try again after callbacks finish."

    return None

def _map_accept_liquidity_failure_message(
    failure: Dict[str, Any],
    *,
    vault_id: str,
    token_contract: str,
) -> Optional[str]:
    """Return a user-friendly mapping for common accept-liquidity failures.

    Note: Most accept failures are "soft" (tokens refunded via ft_on_transfer)
    and won't appear as a transaction Failure. This mapper is for hard failures
    at the FT layer (insufficient balance, receiver not registered, etc.).
    """
    s = _failure_text(failure)

    # Missing 1 yocto on FT call (we attach it, but cover for safety)
    if "Requires attached deposit of exactly 1 yoctoNEAR" in s:
        return (
            "Reason: ft_transfer_call requires exactly 1 yoctoNEAR deposit.\n"
            "Tip: This tool attaches it automatically; please retry."
        )

    # Receiver (vault) not registered in FT storage
    if (
        "is not registered" in s
        or "The account is not registered" in s
        or "storage" in s and "register" in s
    ):
        return (
            "Reason: The vault is not registered with the token contract.\n"
            f"Tip: Use the token registration tool to register `{vault_id}`, then try again."
        )

    # Insufficient FT balance
    if (
        "insufficient" in s.lower()
        or "not enough" in s.lower() and "balance" in s.lower()
        or "Cannot decrement" in s
    ):
        return "Reason: Insufficient token balance to cover the requested amount."

    # Contract or method not found
    if "MethodNotFound" in s or "ContractNotFound" in s or "does not exist" in s:
        return (
            "Reason: Token contract or method not found.\n"
            f"Tip: Ensure `{token_contract}` is correct and implements NEP-141."
        )

    return None


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
                gas=GAS_300_TGAS,
                amount=YOCTO_1,          # 1 yoctoNEAR deposit
            )
        )
        
        # Catch any panic errors
        failure = get_failure_message_from_tx_status(response.status)
        if failure:
            mapped = _map_request_liquidity_panic_message(cast(Dict[str, Any], failure))
            if mapped:
                env.add_reply(
                    "❌ Liquidity Request failed with **contract panic**:\n" + mapped
                )
            else:
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
        
        # Index the vault via backend API (best‑effort)
        try:
            helpers.index_vault_to_firebase(vault_id, response.transaction.hash)
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
    Display all pending liquidity requests in a concise, actionable format.

    • Lists vault, borrower, token (symbol + contract), amounts, duration, collateral.  
    • Adds Explorer links and a quick action to accept the request.  
    • Falls back gracefully when the API errors or returns no data.  
    """

    env = get_env()
    logger = get_logger()

    try:
        # Resolve factory for the active network
        factory_id = get_factory_contract()

        # Fetch pending requests from the web API
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

        explorer = get_explorer_url()
        output_lines: List[str] = ["**📋 Pending Liquidity Requests**\n"]

        for item in pending:
            vault_id = item.get("id", "<unknown>")
            owner = item.get("owner", "unknown")
            lr = item.get("liquidity_request", {})

            # Resolve token metadata for human formatting
            token_contract = lr.get("token")
            if not isinstance(token_contract, str) or not token_contract:
                logger.warning(
                    "Pending liquidity request for %s is missing token contract; skipping entry",
                    vault_id,
                )
                continue
            token_meta = get_token_metadata_by_contract(token_contract)
            decimals = int(token_meta["decimals"])
            symbol = token_meta["symbol"]

            # Format values (no decimals for token/NEAR amounts in list view)
            amount = Decimal(str(lr.get("amount", "0"))) / Decimal(10 ** decimals)
            interest = Decimal(str(lr.get("interest", "0"))) / Decimal(10 ** decimals)
            collateral = Decimal(str(lr.get("collateral", "0"))) / YOCTO_FACTOR
            dur_raw = lr.get("duration")
            duration_days = (int(dur_raw) // 86400) if isinstance(dur_raw, (int, str)) else 0
            term_text = f"{duration_days}d"

            # Estimate APR (simple) like the web view
            apr_text = "N/A"
            try:
                if amount > 0 and duration_days > 0:
                    apr_val = (interest / amount) * Decimal(365) / Decimal(duration_days) * 100
                    apr_text = f"{_format_number(apr_val, 0)}%"
            except (InvalidOperation, DivisionByZero, Overflow, ZeroDivisionError):
                apr_text = "N/A"

            quick_action = f"  • Quick action: `Accept liquidity request opened by {vault_id}`\n"

            output_lines.append(
                (
                    f"- 🏦 [`{vault_id}`]({explorer}/accounts/{vault_id})\n"
                    f"  • Borrower: `{owner}`\n"
                    f"  • Token: {symbol} (`{lr.get('token')}`)\n"
                    f"  • Amount: `{_format_number(amount, 0)}` {symbol} • Interest: `{_format_number(interest, 0)}` {symbol}\n"
                    f"  • Term: `{term_text}`\n"
                    f"  • Collateral: `{_format_number(collateral, 0)}` NEAR\n"
                    f"  • Est. APR: `{apr_text}`\n"
                    f"{quick_action}"
                )
            )

        env.add_reply("\n".join(output_lines))

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
        # Require headless (private-key) signing like request_liquidity
        if signing_mode() != "headless":
            env.add_reply(
                "⚠️ No signing keys available. Add `NEAR_ACCOUNT_ID` and `NEAR_PRIVATE_KEY` to secrets, then try again."
            )
            return

        # Proceed (tests/mocks may not have an account ID).
        lender_id = account_id()

        # Fetch on-chain state to validate preconditions and gather exact terms
        resp_before = run_coroutine(near.view(vault_id, "get_vault_state", {}))
        if not resp_before or not hasattr(resp_before, "result") or resp_before.result is None:
            env.add_reply(f"❌ No data returned for `{vault_id}`. Is the contract deployed?")
            return

        state_before = cast(Dict[str, Any], resp_before.result)
        req = cast(Optional[LiquidityRequest], state_before.get("liquidity_request"))
        offer = state_before.get("accepted_offer")
        owner = state_before.get("owner")

        if not req:
            env.add_reply(f"❌ `{vault_id}` has no active liquidity request to accept.")
            return
        if offer:
            env.add_reply(
                f"❌ `{vault_id}` has no active liquidity request to accept. The request has already been accepted by another lender."
            )
            return
        if isinstance(owner, str) and isinstance(lender_id, str) and lender_id == owner:
            env.add_reply(
                "❌ Vault owner cannot fulfill their own request. Ask another account to accept the request."
            )
            return

        # Prepare ft_transfer_call payload from the exact on-chain terms
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

        # Best-effort balance sanity check (NEP-141 standard). Ignore failures.
        if isinstance(lender_id, str) and lender_id:
            try:
                bal_resp = run_coroutine(
                    near.view(token_contract, "ft_balance_of", {"account_id": lender_id})
                )
                if hasattr(bal_resp, "result") and bal_resp.result is not None:
                    res_val = bal_resp.result
                    have = int(res_val) if isinstance(res_val, (int, str)) else int(str(res_val))
                    need = int(str(token_amount))
                    if have < need:
                        env.add_reply(
                            "❌ Insufficient token balance to accept this request.\n"
                            f"- Token: `{token_contract}`\n"
                            f"- Needed (minimal units): `{need}`\n"
                            f"- Available: `{have}`"
                        )
                        return
            except Exception:
                # Non-standard token or view failure — continue; FT layer will enforce.
                pass

        # Execute ft_transfer_call with 1 yoctoNEAR
        tx: TransactionResult = run_coroutine(
            near.call(
                contract_id=token_contract,
                method_name="ft_transfer_call",
                args={
                    "receiver_id": vault_id,
                    "amount": token_amount,
                    "msg": json.dumps(msg_payload),
                },
                gas=GAS_300_TGAS,
                amount=YOCTO_1,
            )
        )

        # Map hard failures (FT contract level)
        failure = get_failure_message_from_tx_status(tx.status)
        if failure:
            mapped = _map_accept_liquidity_failure_message(failure, vault_id=vault_id, token_contract=token_contract)
            if mapped:
                env.add_reply(
                    "❌ Failed to accept liquidity request\n" + mapped
                )
            else:
                env.add_reply(
                    "❌ Failed to accept liquidity request\n\n" + f"> {json.dumps(failure, indent=2)}"
                )
            return

        # Success path: transaction executed without Failure.

        # Index accepted vault via backend API (best effort)
        try:
            helpers.index_vault_to_firebase(vault_id, tx.transaction.hash)
        except Exception as e:
            logger.warning("index_vault_to_firebase failed: %s", e, exc_info=True)

        # Human-friendly amounts
        token_meta = get_token_metadata_by_contract(token_contract)
        decimals = int(token_meta["decimals"])
        symbol = token_meta["symbol"]
        principal = Decimal(str(req["amount"])) / Decimal(10 ** decimals)
        interest_amt = Decimal(str(req["interest"])) / Decimal(10 ** decimals)
        collateral_near = Decimal(str(req["collateral"])) / YOCTO_FACTOR
        dur_val3 = req.get("duration") if isinstance(req, dict) else None
        duration_days = (int(dur_val3) // 86400) if isinstance(dur_val3, (int, str)) else 0

        apr_text = "N/A"
        try:
            if principal > 0 and duration_days > 0:
                apr_val = (interest_amt / principal) * Decimal(365) / Decimal(duration_days) * 100
                apr_text = f"{_format_number(apr_val, 2)}%"
        except (InvalidOperation, DivisionByZero, Overflow, ZeroDivisionError):
            apr_text = "N/A"

        accepted_ts_text = "on-chain"

        explorer = get_explorer_url()
        env.add_reply(
            f"✅ **Accepted Liquidity Request**\n"
            f"- 🏦 Vault: [`{vault_id}`]({explorer}/accounts/{vault_id})\n"
            f"- 🪙 Token: `{token_contract}`\n"
            f"- 💵 Principal: `{_format_number(principal, 0)}` {symbol} • Interest: `{_format_number(interest_amt, 0)}` {symbol} • APR: {apr_text}\n"
            f"- 💰 Collateral: `{_format_number(collateral_near, 0)}` NEAR\n"
            f"- ⏳ Term: `{duration_days} days`\n"
            f"- 🕒 Accepted: `{accepted_ts_text}`\n"
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
            blocks.append(_format_position_entry(explorer, entry, preloaded_state=pre_state))

        env.add_reply("".join(blocks))

    except Exception as e:
        logger.warning("view_lender_positions failed: %s", e, exc_info=True)
        env.add_reply(f"❌ Failed to fetch lending positions\n\n**Error:** {e}")
