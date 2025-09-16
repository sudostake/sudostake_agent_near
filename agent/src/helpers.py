import os
import asyncio
import requests

from nearai.agents.environment import Environment
from typing import Awaitable, TypeVar, Optional, Any, List, Dict, cast
from near_types import NearClient
from decimal import Decimal
from datetime import datetime, timezone
from constants import NANOSECONDS_PER_SECOND

# Type‐var for our coroutine runner
T = TypeVar("T")

# ──────────────────────────────────────────────────────────────
# GLOBAL STATE
# ──────────────────────────────────────────────────────────────
# TODO move to fastnear.com
_DEFAULT_RPC = {
    "mainnet": "https://rpc.mainnet.near.org",
    "testnet": "https://rpc.testnet.near.org",
}

_EXPLORER_URL = {
    "mainnet": "https://explorer.near.org",
    "testnet": "https://explorer.testnet.near.org",
}

# Factory contract addresses per network
_FACTORY_CONTRACTS = {
    "mainnet": "sudostake.near",
    "testnet": "nzaza.testnet",
}

# USDC contract addresses per network
USDC_CONTRACTS = {
    "mainnet": "17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1",
    "testnet": "usdc.tkn.primitives.testnet",
}

# Backend API base (fixed)
_FIREBASE_VAULTS_API = "http://v0-sudo-stake-near-web.vercel.app/api"

# Define current vault_minting_fee
# TODO Later we can dynamically get this from the factory contract itself
VAULT_MINT_FEE_NEAR: Decimal = Decimal("10")

# NEAR uses 10^24 yoctoNEAR per 1 NEAR
YOCTO_FACTOR: Decimal = Decimal("1e24")

# USDC uses 10^6 for 1 USDC
USDC_FACTOR: Decimal = Decimal("1e6")

_loop: Optional[asyncio.AbstractEventLoop] = None
"""Mutable module state for the current signing context."""
_signing_mode: Optional[str] = None       # "headless", "wallet", or None
_account_id: Optional[str] = None         # the user’s account when known
_VECTOR_STORE_ID: str = "vs_ecd9ba192396493984d66feb" # default vector store ID


# expose handy getters
def signing_mode()    -> Optional[str]: return _signing_mode
def account_id()      -> Optional[str]: return _account_id
def vector_store_id() -> str:
    """Return the vector-store ID."""
    return _VECTOR_STORE_ID
def firebase_vaults_api() -> str:       return _FIREBASE_VAULTS_API
# ──────────────────────────────────────────────────────────────

def usdc_contract() -> str:
    """
    Return the USDC contract address for the current NEAR_NETWORK.

    We don't have to check for the environment variable here,
    as this function is only called after the NEAR_NETWORK is set
    in the environment.
    """
    network = os.getenv("NEAR_NETWORK")
    if network not in USDC_CONTRACTS:
        raise RuntimeError(
            "NEAR_NETWORK must be set to 'mainnet' or 'testnet' (got: "
            f"{network or 'unset'})"
        )
    return USDC_CONTRACTS[network]


def fetch_usdc_balance(near: NearClient, account_id: str) -> Decimal:
    """
    Retrieve and return the USDC balance (as a Decimal) for the given account ID.

    Raises:
        ValueError: if the view call fails or no result is returned.
    """

    resp = run_coroutine(
        near.view(usdc_contract(), "ft_balance_of", {"account_id": account_id})
    )

    if not resp or not hasattr(resp, "result") or resp.result is None:
        raise ValueError(f"❌ No USDC balance returned for `{account_id}`.")

    usdc_raw = int(resp.result)
    return Decimal(usdc_raw) / USDC_FACTOR


def get_explorer_url() -> str:
    """
    Return the correct NEAR Explorer URL based on NEAR_NETWORK.
    """
    network = os.getenv("NEAR_NETWORK")
    if network not in _EXPLORER_URL:
        raise RuntimeError(
            "NEAR_NETWORK must be set to 'mainnet' or 'testnet' (got: "
            f"{network or 'unset'})"
        )
    return _EXPLORER_URL[network]


def get_rpc_addr(network: Optional[str] = None) -> str:
    """Return the NEAR RPC endpoint for the active network.

    If ``network`` is None, reads ``NEAR_NETWORK`` from the environment.
    Raises a RuntimeError when the value is missing or invalid, matching
    the messaging used elsewhere in helpers.
    """
    net = network or os.getenv("NEAR_NETWORK")
    if net not in _DEFAULT_RPC:
        raise RuntimeError(
            "NEAR_NETWORK must be set to 'mainnet' or 'testnet' (got: "
            f"{net or 'unset'})"
        )
    return _DEFAULT_RPC[net]


def get_factory_contract() -> str:
    """
    Return the factory contract address for the current NEAR_NETWORK.

    We don't have to check for the environment variable here,
    as this function is only called after the NEAR_NETWORK is set
    in the environment.
    """
    network = os.getenv("NEAR_NETWORK")
    if network not in _FACTORY_CONTRACTS:
        raise RuntimeError(
            "NEAR_NETWORK must be set to 'mainnet' or 'testnet' (got: "
            f"{network or 'unset'})"
        )
    return _FACTORY_CONTRACTS[network]


def ensure_loop() -> asyncio.AbstractEventLoop:
    """Return a long-lived event loop, creating it once if necessary."""

    global _loop

    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def run_coroutine(coroutine: Awaitable[T]) -> T:
    """
    Helper to run an async coroutine on the shared event loop.
    """
    return ensure_loop().run_until_complete(coroutine)


def _set_state(mode: Optional[str], acct: Optional[str]) -> None:
    global _signing_mode, _account_id
    _signing_mode, _account_id = mode, acct

def propagate_signing_state(mode: Optional[str], acct: Optional[str]) -> None:
    """Public helper to update signing context for tools that rely on helpers.* getters."""
    _set_state(mode, acct)


def init_near(env: Environment) -> NearClient:
    """
    Create a py-near Account.

    * headless  - secret key in env   → signing_mode = 'headless'
    * view-only - no key / wallet     → signing_mode None
    """

    # Check for required NEAR_NETWORK env variable
    network = os.getenv("NEAR_NETWORK")
    if network not in _DEFAULT_RPC:
        raise RuntimeError(
            "NEAR_NETWORK must be set to 'mainnet' or 'testnet' (got: "
            f"{network or 'unset'})"
        )

    account_id  = os.getenv("NEAR_ACCOUNT_ID")
    private_key = os.getenv("NEAR_PRIVATE_KEY")
    rpc_addr    = _DEFAULT_RPC[network]

    # For headless signing, we need both account_id and private_key
    if account_id and private_key:
        near = cast(NearClient, env.set_near(
            account_id=account_id,
            private_key=private_key,
            rpc_addr=rpc_addr
        ))
        _set_state(mode="headless", acct=account_id)
        return near

    # view-only fallback
    signer = getattr(env, "signer_account_id", None)
    _set_state(mode=None, acct=signer)
    near = cast(NearClient, env.set_near(rpc_addr=rpc_addr))
    return near


def get_failure_message_from_tx_status(status: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    failure = status.get("Failure")
    if failure:
        action_err = failure.get("ActionError", {})
        return cast(Dict[str, Any], action_err.get("kind", {}))
    return None


def log_contains_event(logs: list[str], event_name: str) -> bool:
    """
    Returns True if any log contains the given event name.
    Supports plain or EVENT_JSON logs.
    """

    for log in logs:
        if event_name in log:
            return True
    return False


def _parse_event_json(log: str) -> Optional[Dict[str, Any]]:
    """Try to parse a single EVENT_JSON log line into a dict.

    Expected format: 'EVENT_JSON: { "event": "...", "data": { ... } }'
    Returns None if parsing fails.
    """
    try:
        if "EVENT_JSON:" not in log:
            return None
        # Split at first occurrence to be robust to prefixes
        _, json_part = log.split("EVENT_JSON:", 1)
        json_part = json_part.strip()
        if not json_part:
            return None
        return cast(Dict[str, Any], __import__("json").loads(json_part))
    except Exception:
        return None


def find_event_data(logs: list[str], event_name: str) -> Optional[Dict[str, Any]]:
    """Return the `data` payload for the first EVENT_JSON with matching `event`.

    Falls back to None when no structured event is found.
    """
    for log in logs:
        rec = _parse_event_json(log)
        if not rec:
            continue
        if rec.get("event") == event_name:
            data = rec.get("data")
            if isinstance(data, dict):
                return cast(Dict[str, Any], data)
            return {}
    return None


def top_doc_chunks(env: Environment, vs_id: str, user_query: str, k: int = 6) -> List[Dict[str, Any]]:
    """
    Return the top-k vector-store chunks for *user_query*.
    Does not touch env.add_reply(); safe for reuse.
    """

    results = env.query_vector_store(vs_id, user_query)
    return results[:k]  # trim noise


def index_vault_to_firebase(vault_id: str, tx_hash: str, factory_id: Optional[str] = None) -> None:
    """
    Index the given vault via the backend API.

    Raises:
        Exception: If the request fails or Firebase responds with an error.
    """

    # Resolve factory if not explicitly provided
    if factory_id is None:
        factory_id = get_factory_contract()

    idx_url = f"{_FIREBASE_VAULTS_API}/index_vault"

    payload = {
        "factory_id": factory_id,
        "vault": vault_id,
        "tx_hash": tx_hash,
    }

    response = requests.post(
        idx_url,
        json=payload,
        timeout=10,
        headers={"Content-Type": "application/json"},
    )
    response.raise_for_status()


def format_near_timestamp(ns: int) -> str:
    """Convert NEAR block timestamp (ns since epoch) to a readable UTC datetime."""
    ts = ns / NANOSECONDS_PER_SECOND
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


from typing import Union

def format_firestore_timestamp(ts: Union[Dict[str, Any], str]) -> str:
    """Convert Firestore timestamp (dict or string) to 'YYYY-MM-DD HH:MM UTC'."""
    if isinstance(ts, str):
        return ts
    dt = datetime.fromtimestamp(ts["_seconds"], tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")
