"""Small helpers for tests (non-pytest fixtures)."""

def make_dummy_resp(json_body):
    """Minimal stub mimicking requests.Response for our needs."""
    class DummyResp:
        def raise_for_status(self):          # no-op ⇢ 200 OK
            pass
        def json(self):
            return json_body
    return DummyResp()


def failure_exec_error(message: str) -> dict:
    """Build a tx status dict that encodes a FunctionCallError.ExecutionError.

    Many tests need to simulate NEAR transaction failures with a specific
    ExecutionError string; centralize the structure here for reuse.
    """
    return {
        "Failure": {
            "ActionError": {
                "kind": {
                    "FunctionCallError": {"ExecutionError": message}
                }
            }
        }
    }
