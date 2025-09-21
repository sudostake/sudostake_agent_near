import os
from decimal import Decimal
from logging import Logger
from .context import get_env, get_near, get_logger
from helpers import (
    YOCTO_FACTOR,
    signing_mode,
    run_coroutine,
    get_explorer_url,
    is_rpc_connectivity_error,
)
from py_near.models import TransactionResult

def transfer_near_to_vault(vault_id: str, amount: str) -> None:
    """
    Transfer `amount` NEAR from the main wallet to `vault_id`.

    • Head-less signing required (NEAR_ACCOUNT_ID & NEAR_PRIVATE_KEY).
    • Uses py-near `send_money` (amount must be in yocto).
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
    
    # Normalize inputs
    vault_id = (vault_id or "").strip()
    amount_str = (amount or "").strip()

    # Basic vault account check
    if not vault_id:
        env.add_reply("❌ Invalid vault account: value is empty.")
        return

    # Parse amount (NEAR → yocto) and validate positivity
    try:
        dec_amt = Decimal(amount_str)
        if dec_amt <= 0:
            env.add_reply(
                "❌ Amount must be greater than 0.\n"
                "Examples: `0.5`, `2`, `10.25`"
            )
            return
        yocto = int((dec_amt * YOCTO_FACTOR).quantize(Decimal("1")))
    except Exception:
        env.add_reply(
            f"❌ Invalid amount: {amount!r}\n"
            "Enter a positive number like `0.5`, `2`, or `10.25`."
        )
        return
    
    try:
        tx: TransactionResult = run_coroutine(
            near.send_money(account_id=vault_id, amount=yocto)
        )
        
        tx_hash  = tx.transaction.hash
        explorer = get_explorer_url()
        gas_burnt = getattr(getattr(tx, "transaction_outcome", None), "gas_burnt", None)
        gas_line = (
            f"\n⛽ Gas Burned: {gas_burnt / 1e12:.2f} Tgas"
            if (isinstance(gas_burnt, (int, float)) and gas_burnt > 0)
            else ""
        )
        
        env.add_reply(
            "💸 **Transfer Submitted**\n"
            f"Sent **{dec_amt:.5f} NEAR** to `{vault_id}`.\n"
            f"🔹 Account: [{vault_id}]({explorer}/accounts/{vault_id})\n"
            f"🔹 Tx: [{tx_hash}]({explorer}/transactions/{tx_hash})"
            f"{gas_line}"
        )
        
    except Exception as e:
        logger.error(
            "transfer_near_to_vault error → %s (%s NEAR): %s",
            vault_id, amount_str, e, exc_info=True
        )
        # Optional connectivity hint (DNS/RPC outages or wrong network)
        hint = None
        if is_rpc_connectivity_error(e):
            want_testnet = vault_id.endswith(".testnet")
            suggested_network = "testnet" if want_testnet else "mainnet"
            current_net = os.getenv("NEAR_NETWORK") or "unset"
            hint = (
                "\n\n📡 RPC appears unreachable.\n"
                f"- Current NEAR_NETWORK: `{current_net}` (vault looks like `{suggested_network}`)\n"
                f"- Tip: set `NEAR_NETWORK={suggested_network}` for this vault.\n"
                "- Check your network/DNS and retry shortly."
            )

        env.add_reply(
            f"❌ Transfer failed for `{vault_id}` ({amount_str} NEAR)\n\n**Error:** {e}" + (hint or "")
        )
