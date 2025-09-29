import json
from decimal import Decimal
from logging import Logger
from .context import get_env, get_near, get_logger
from helpers import (
    YOCTO_FACTOR,
    VAULT_MINT_FEE_NEAR,
    get_factory_contract,
    index_vault_to_firebase,
    signing_mode,
    run_coroutine,
    get_failure_message_from_tx_status,
    get_explorer_url,
)
from py_near.models import TransactionResult
from runtime_constants import GAS_300_TGAS


def mint_vault() -> None:
    """
    Mint a new SudoStake vault.

    • Head-less signing required (NEAR_ACCOUNT_ID + NEAR_PRIVATE_KEY).  
    • Uses the fixed 10 NEAR fee ( `VAULT_MINT_FEE_NEAR` ).  
    • Factory account is derived from `NEAR_NETWORK`.
    """
    
    env = get_env()
    near = get_near()
    logger: Logger = get_logger()
    
    # 'headless' or None
    if signing_mode() != "headless":
        env.add_reply(
            "⚠️ I can't sign transactions in this session.\n "
            "Add `NEAR_ACCOUNT_ID` and `NEAR_PRIVATE_KEY` to your run's "
            "secrets, then try again."
        )
        return
    
    # Prepare call params
    factory_id = get_factory_contract()
    yocto_fee  = int((VAULT_MINT_FEE_NEAR * YOCTO_FACTOR).quantize(Decimal('1')))
    
    try:
        # Perform the payable delegate call with yocto_fee attached
        response: TransactionResult = run_coroutine(
            near.call(
                contract_id=factory_id,
                method_name="mint_vault",
                args={},
                gas=GAS_300_TGAS,
                amount=yocto_fee,               # 10 NEAR in yocto
            )
        )
        
        # Inspect execution outcome for Failure / Panic
        failure = get_failure_message_from_tx_status(response.status)
        if failure:
            env.add_reply(
                "❌ Mint vault failed with **contract panic**:\n\n"
               f"> {json.dumps(failure, indent=2)}"
            )
            return
        
        # Extract tx_hash from the response
        tx_hash  = response.transaction.hash
        explorer = get_explorer_url()
        
        # Extract new vault account from EVENT_JSON log
        vault_acct = None
        for log in response.logs:
            if log.startswith("EVENT_JSON:"):
                payload = json.loads(log.split("EVENT_JSON:")[1])
                if payload.get("event") == "vault_minted":
                    vault_acct = payload["data"]["vault"]
                    break
            
        if vault_acct is None:
            raise RuntimeError("vault_minted log not found in transaction logs")
        
        # Index the vault via backend API
        try:
            index_vault_to_firebase(vault_acct, tx_hash)
        except Exception as e:
            logger.warning("index_vault_to_firebase failed: %s", e, exc_info=True)
        
        env.add_reply(
            "🏗️ **Vault Minted**\n"
            f"🔑 Vault account: [`{vault_acct}`]({explorer}/accounts/{vault_acct})\n"
            f"🔹 Tx: [{tx_hash}]({explorer}/transactions/{tx_hash})"
        )
    
    except Exception as e:
        # Try to render a more human-friendly message for insufficient balance
        try:
            # py-near or downstream libs may raise a dict as the first arg
            details = None
            if isinstance(e, dict):  # rare, but cheap to check
                details = e
            elif getattr(e, "args", None):
                first = e.args[0]
                if isinstance(first, dict):
                    details = first
                else:
                    # Attempt safe parse from a stringified Python dict
                    s = str(first)
                    if "balance" in s and "cost" in s:
                        import ast
                        try:
                            parsed = ast.literal_eval(s)
                            if isinstance(parsed, dict):
                                details = parsed
                        except Exception:
                            details = None

            if isinstance(details, dict) and "balance" in details and "cost" in details:
                bal_raw = details.get("balance")
                cost_raw = details.get("cost")
                signer_val = details.get("signer_id")
                signer = str(signer_val) if signer_val is not None else "unknown"

                if isinstance(bal_raw, (int, str)) and isinstance(cost_raw, (int, str)):
                    bal = Decimal(str(int(bal_raw))) / YOCTO_FACTOR
                    req = Decimal(str(int(cost_raw))) / YOCTO_FACTOR
                    short = max(Decimal(0), req - bal)
                    env.add_reply(
                        "❌ Insufficient NEAR to mint vault.\n"
                        f"- 👤 Account: `{signer}`\n"
                        f"- Available: **{bal:.5f} NEAR**\n"
                        f"- Required: **{req:.5f} NEAR**\n"
                        f"- Shortfall: **{short:.5f} NEAR**\n\n"
                        "The minting fee is 10 NEAR plus gas. Top up and retry."
                    )
                    return
        except Exception:
            # Fall through to generic handling if pretty formatting fails
            pass

        logger.error("mint_vault error: %s", e, exc_info=True)
        env.add_reply(f"❌ Vault minting failed\n\n**Error:** {e}")
