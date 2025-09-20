import json
from nearai.agents.environment import Environment
from helpers import ensure_loop, init_near, vector_store_id, top_doc_chunks


def run(env: Environment) -> None:
    """
    Minimal, single-source entrypoint:
    - Ensure loop, initialize NEAR via helpers.
    - Register tools via tools.base.
    - Assemble prompt with optional docs grounding.
    - Always respond; surface concise diagnostics on failure.
    """

    # Event loop + NEAR init
    ensure_loop()
    try:
        near = init_near(env)
    except Exception as e:
        env.add_reply(
            "Failed to initialize NEAR. Set NEAR_NETWORK and optionally headless creds.\n"
            f"Error: {e}"
        )
        return

    # Register tools (import lazily and guard errors so we always reply)
    try:
        from tools import register_tools  # import-inside to avoid import-time failures breaking agent
        tool_defs = register_tools(env, near)
    except Exception as e:
        env.add_reply(
            "Failed to register tools. This can happen if there is a bad import or a circular dependency.\n"
            f"Error: {e}"
        )
        return

    # Build prompt (system → history → docs → latest user)
    messages = env.list_messages()
    history = messages[:-1] if len(messages) > 1 else []
    latest = [messages[-1]] if messages else []

    # Best-effort docs grounding
    docs = []
    try:
        user_query = latest[-1]["content"] if latest else ""
        if user_query:
            docs = top_doc_chunks(env, vector_store_id(), user_query)
    except Exception:
        docs = []

    prompt_list = [
        {
            "role": "system",
            "content": (
                "You are SudoStake's AI Agent. "
                "If documentation is provided, use it as your primary source. "
                "Ground answers in the docs; do not invent facts. "
                "If the docs lack detail, say so and suggest the docs tool."
            ),
        },
        *history,
        *([{ "role": "documentation", "content": json.dumps(docs)}] if docs else []),
        *latest,
    ]

    try:
        env.completions_and_run_tools(prompt_list, tools=tool_defs)
    except Exception as e:
        env.add_reply(
            "The assistant encountered an error while generating a reply.\n"
            f"Error: {e}"
        )


# Only invoke run(env) if NearAI has injected `env` at import time.
if "env" in globals():
    run(env)  # type: ignore[name-defined]
