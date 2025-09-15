import os
import asyncio
import json
from typing import Any, Dict, List
from nearai.agents.environment import Environment


def run(env: Environment) -> None:
    """
    Agent entrypoint with strict, incremental setup:
    - Ensure event loop and initialize NEAR (headless or view-only).
    - Lazily register tools to avoid import-time issues and circulars.
    - Best-effort docs context from vector store; never blocks responses.
    - Graceful degradation: on any failure, reply with a concise diagnostic.
    """

    # Ensure an event loop exists
    try:
        asyncio.get_event_loop()
    except Exception:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    # Initialize NEAR directly (avoid helpers to prevent circular imports)
    near_client: Any = None
    near_status = "uninitialized"
    try:
        _DEFAULT_RPC = {
            "mainnet": "https://rpc.mainnet.near.org",
            "testnet": "https://rpc.testnet.near.org",
        }
        network = os.getenv("NEAR_NETWORK")
        if network not in _DEFAULT_RPC:
            raise RuntimeError(
                f"NEAR_NETWORK must be set to 'mainnet' or 'testnet' (got: {network or 'unset'})"
            )
        rpc_addr = _DEFAULT_RPC[network]
        account_id = os.getenv("NEAR_ACCOUNT_ID")
        private_key = os.getenv("NEAR_PRIVATE_KEY")

        if account_id and private_key:
            near_client = env.set_near(account_id=account_id, private_key=private_key, rpc_addr=rpc_addr)
            mode = "headless"
            acct = account_id
        else:
            near_client = env.set_near(rpc_addr=rpc_addr)
            mode = "view-only"
            acct = getattr(env, "signer_account_id", None) or "(none)"

        near_status = f"initialized (mode: {mode}, account: {acct})"
        # Attempt to propagate signing state to helpers for tools that rely on it.
        try:
            import helpers as _helpers  # type: ignore
            try:
                _helpers.propagate_signing_state(
                    mode=mode if mode == "headless" else None,
                    acct=acct if mode == "headless" else None,
                )
            except Exception:
                pass
        except Exception:
            # If helpers import fails, tools relying on helpers.signing_mode may show a warning.
            pass
    except Exception as e:
        near_status = f"failed to init: {e}"

    messages: List[Dict[str, str]] = env.list_messages()
    last: str = messages[-1]["content"] if messages else ""
    text = (last or "").strip().lower()

    # Incremental step: lazily register tools and run a simple completion.
    # Skip vector-store. Fall back to a direct reply on any error.
    try:
        # Only proceed if NEAR initialized successfully
        if not near_status.startswith("initialized"):
            raise RuntimeError(near_status)

        try:
            from tools import register_tools  # type: ignore
        except Exception as e:
            raise RuntimeError(f"import tools failed: {e}")

        try:
            if near_client is None:
                raise RuntimeError("NEAR client unavailable")
            tool_defs = register_tools(env, near_client)
        except Exception as e:
            raise RuntimeError(f"register_tools failed: {e}")

        # Incremental addition: best-effort docs context from vector store
        docs = []
        try:
            try:
                import helpers as _helpers  # type: ignore
                vs_id = _helpers.vector_store_id()  # honors SUDOSTAKE_VECTOR_STORE_ID
            except Exception:
                vs_id = os.getenv("SUDOSTAKE_VECTOR_STORE_ID", "vs_ecd9ba192396493984d66feb")

            if vs_id and (last or "").strip():
                res = env.query_vector_store(vs_id, last)
                docs = (res or [])[:6]
        except Exception:
            docs = []

        # Build prompt: system → prior history → docs (if any) → latest user
        history: List[Dict[str, str]] = messages[:-1] if len(messages) > 1 else []
        prompt_list = [
            {"role": "system", "content": (
                "You are SudoStake's AI Agent. "
                "If a documentation message is provided, use it as your primary source. "
                "Ground answers in the docs; do not invent facts. "
                "If the docs lack the required detail, state that clearly and suggest using the docs tool."
            )},
            *history,
            *([{ "role": "documentation", "content": json.dumps(docs)}] if docs else []),
            {"role": "user", "content": last or ""},
        ]

        try:
            env.completions_and_run_tools(prompt_list, tools=tool_defs)
            return
        except Exception as e:
            raise RuntimeError(f"tool run failed: {e}")

    except Exception as e:
        env.add_reply(
            f"Tools not active: {e}\nNEAR: {near_status}"
        )


# Only invoke run(env) if NearAI has injected `env` at import time.
if "env" in globals():
    run(env)  # type: ignore[name-defined]
