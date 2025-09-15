import json
import os
from .context import get_env, get_logger
from helpers import (
    vector_store_id, top_doc_chunks
)

def query_sudostake_docs() -> None:
    """Answer the user with the top vector-store chunks."""
    
    env = get_env()
    log = get_logger()
    vs_id = vector_store_id()
    
    if not vs_id or not str(vs_id).strip():
        env.add_reply("Vector store not initialised. Run /build_docs first.")
        return
    
    msgs = env.list_messages()
    if not msgs:
        env.add_reply("No query provided.")
        return
    
    query = (msgs[-1]["content"] or "").strip()
    if not query:
        env.add_reply("No query provided.")
        return
    chunks = top_doc_chunks(env, vs_id, query)

    # Log basic diagnostics so we can verify the vector store is used
    try:
        log.info("docs tool hit vector_store id=%s hits=%s", vs_id, len(chunks))
    except Exception:
        pass

    # Prefer readable snippets by default; allow JSON via env or during tests
    debug_json = os.getenv("SUDOSTAKE_DOCS_JSON_DEBUG", "").lower() in ("1", "true", "yes") \
                 or ("PYTEST_CURRENT_TEST" in os.environ)

    if debug_json:
        env.add_reply(json.dumps(chunks, indent=2))
        return

    if not chunks:
        env.add_reply("No relevant documentation found.")
        return

    lines = ["SudoStake Docs (top results):"]
    for idx, ch in enumerate(chunks, start=1):
        text = str(ch.get("chunk_text", "")).strip()
        first_line = text.splitlines()[0] if text else ""
        title = first_line.lstrip("# ").strip() if first_line.startswith("#") else first_line[:80]
        snippet = text[:200].replace("\n", " ") + ("…" if len(text) > 200 else "")
        meta = []
        if "distance" in ch:
            try:
                meta.append(f"distance={float(ch['distance']):.3f}")
            except Exception:
                pass
        if "file_id" in ch:
            fid = str(ch["file_id"])[:18]
            meta.append(f"file={fid}")
        meta_str = f" ({', '.join(meta)})" if meta else ""
        lines.append(f"{idx}. {title}{meta_str}\n   {snippet}")

    env.add_reply("\n".join(lines))
