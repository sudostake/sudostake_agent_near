from unittest.mock import AsyncMock, MagicMock
from tools import process_claims as pc
from tools import active_loan
import helpers
import json


def event_json(event, data=None):
    payload = {"event": event}
    if data is not None:
        payload["data"] = data
    return f"EVENT_JSON:{json.dumps(payload)}"


def test_process_claims_no_credentials(monkeypatch, mock_setup):
    """Should warn and return when no signing keys are available."""

    env, _ = mock_setup

    # Ensure signing is disabled
    monkeypatch.setattr(helpers, "_signing_mode", None, raising=False)

    pc.process_claims("vault-xyz.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "No signing keys" in msg


def test_process_claims_not_allowed_until_timestamp(monkeypatch, mock_setup):
    """Should map 'not allowed until <ts>' panic into a readable message."""

    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setenv("NEAR_NETWORK", "testnet")
    # Stick to active_loan for time formatting so monkeypatch keeps working
    monkeypatch.setattr(active_loan, "format_near_timestamp", lambda ns: "2025-01-01 00:00 UTC")

    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx123"),
        logs=[],
        status={
            "Failure": {
                "ActionError": {
                    "kind": {
                        "FunctionCallError": {"ExecutionError": "Liquidation not allowed until 1700000000000000000"}
                    }
                }
            }
        },
    ))

    pc.process_claims("vault-7.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Liquidation not allowed yet" in msg
    assert "2025-01-01 00:00 UTC" in msg


def test_process_claims_contract_panic_generic(monkeypatch, mock_setup):
    """Should surface generic panic JSON when not matched to mapping."""

    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)

    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="txabc"),
        logs=[],
        status={
            "Failure": {
                "ActionError": {
                    "kind": {
                        "FunctionCallError": {"ExecutionError": "unexpected"}
                    }
                }
            }
        }
    ))

    pc.process_claims("vault-a.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Processing claims failed" in msg
    assert "contract panic" in msg


def test_process_claims_completed_success(monkeypatch, mock_setup):
    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setenv("NEAR_NETWORK", "testnet")

    logs = ["liquidation_complete"]
    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_done"),
        logs=logs,
        status={"SuccessValue": ""},
    ))

    pc.process_claims("vault-done.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Liquidation Complete" in msg
    assert "tx_done" in msg


def test_process_claims_progress_with_details(monkeypatch, mock_setup):
    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setenv("NEAR_NETWORK", "testnet")

    logs = [
        event_json("liquidation_started", {"lender": "alice.testnet", "at": "1700000000000000000"}),
        event_json("liquidation_progress", {"reason": "awaiting unstake"}),
        event_json("unstake_recorded", {"validator": "val.poolv1.near", "amount": "1000000", "epoch_height": 424242}),
    ]

    # Patch time format via active_loan and amounts readable by default logic
    monkeypatch.setattr(active_loan, "format_near_timestamp", lambda ns: "2025-01-02 03:04 UTC")

    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_prog"),
        logs=logs,
        status={"SuccessValue": ""},
    ))

    pc.process_claims("vault-prog.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Claims Processing In Progress" in msg
    assert "Liquidation started" in msg
    assert "alice.testnet" in msg
    assert "2025-01-02 03:04 UTC" in msg
    assert "Unstake recorded" in msg


def test_process_claims_generic_success_no_logs(monkeypatch, mock_setup):
    """Should provide a generic success message when no actionable logs are present."""

    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setenv("NEAR_NETWORK", "testnet")

    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_ok"),
        logs=[],
        status={"SuccessValue": ""},
    ))

    pc.process_claims("vault-g.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Processed claims step" in msg


def test_process_claims_indexing_failure_does_not_block(monkeypatch, mock_setup):
    """Indexing errors should not prevent a normal success reply."""

    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setenv("NEAR_NETWORK", "testnet")

    logs = ["liquidation_complete"]
    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_idx"),
        logs=logs,
        status={"SuccessValue": ""},
    ))

    def raise_index_error(vault_id, tx_hash):
        raise Exception("index failure")

    monkeypatch.setattr(active_loan, "index_vault_to_firebase", raise_index_error)

    pc.process_claims("vault-idx.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Liquidation Complete" in msg
    assert "tx_idx" in msg


def test_process_claims_runtime_error(monkeypatch, mock_setup):
    """Unexpected exceptions from near.call should be handled gracefully."""

    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)

    mock_near.call = AsyncMock(side_effect=RuntimeError("boom"))

    pc.process_claims("vault-err.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Unexpected error" in msg
    assert "boom" in msg


# ───────────────── additional coverage for process_claims ─────────────────

def _failure_with_exec_error(message: str) -> dict:
    return {
        "Failure": {
            "ActionError": {
                "kind": {
                    "FunctionCallError": {"ExecutionError": message}
                }
            }
        }
    }


def test_process_claims_lock_busy_mapping_no_quotes(monkeypatch, mock_setup):
    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)

    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_busy"),
        logs=[],
        status=_failure_with_exec_error("Vault busy with ProcessClaims"),
    ))

    pc.process_claims("vault-busy.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Vault is busy" in msg
    assert "ProcessClaims" in msg


def test_process_claims_lock_busy_mapping_with_quotes(monkeypatch, mock_setup):
    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)

    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_busy2"),
        logs=[],
        status=_failure_with_exec_error('Vault busy with "RepayLoan"'),
    ))

    pc.process_claims("vault-busy2.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Vault is busy" in msg
    assert "RepayLoan" in msg


def test_process_claims_missing_one_yocto(monkeypatch, mock_setup):
    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)

    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_yocto"),
        logs=[],
        status=_failure_with_exec_error("Requires attached deposit of exactly 1 yoctoNEAR"),
    ))

    pc.process_claims("vault-yocto.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "1 yoctoNEAR" in msg
    assert "attaches it automatically" in msg


def test_process_claims_no_accepted_offer(monkeypatch, mock_setup):
    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)

    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_no_offer"),
        logs=[],
        status=_failure_with_exec_error("No accepted offer found"),
    ))

    pc.process_claims("vault-nooffer.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "No active loan" in msg or "No accepted offer" in msg


def test_process_claims_completion_event_json_shows_total_repaid(monkeypatch, mock_setup):
    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setenv("NEAR_NETWORK", "testnet")

    logs = [
        event_json("liquidation_complete", {"total_repaid": "5000000000000000000000000"})
    ]

    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_done_json"),
        logs=logs,
        status={"SuccessValue": ""},
    ))

    pc.process_claims("vault-done-json.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Liquidation Complete" in msg
    assert "Total repaid: `5000000000000000000000000` yoctoNEAR" in msg


def test_process_claims_started_event_json_shows_lender_and_time(monkeypatch, mock_setup):
    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setenv("NEAR_NETWORK", "testnet")
    monkeypatch.setattr(active_loan, "format_near_timestamp", lambda ns: "2025-01-02 03:04 UTC")

    logs = [
        event_json("liquidation_started", {"lender": "alice.testnet", "at": "1700000000000000000"}),
        event_json("liquidation_progress", {"reason": "awaiting unstake"}),
    ]

    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_started_json"),
        logs=logs,
        status={"SuccessValue": ""},
    ))

    pc.process_claims("vault-start.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Liquidation started" in msg
    assert "alice.testnet" in msg
    assert "2025-01-02 03:04 UTC" in msg


def test_process_claims_unstake_failed_event_json_details(monkeypatch, mock_setup):
    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setenv("NEAR_NETWORK", "testnet")

    logs = [
        event_json("liquidation_progress", {"reason": "awaiting unstake"}),
        event_json("unstake_failed", {"validator": "val.poolv1.near", "amount": "42"}),
    ]

    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_uf_json"),
        logs=logs,
        status={"SuccessValue": ""},
    ))

    pc.process_claims("vault-uf.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "unstake attempt failed" in msg
    assert "val.poolv1.near" in msg
    assert "Amount: `42` yoctoNEAR" in msg


def test_process_claims_progress_without_reason(monkeypatch, mock_setup):
    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setenv("NEAR_NETWORK", "testnet")

    logs = [
        event_json("liquidation_progress", {}),
    ]

    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_prog_noreason"),
        logs=logs,
        status={"SuccessValue": ""},
    ))

    pc.process_claims("vault-prog2.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Claims Processing In Progress" in msg
    assert "Reason:" not in msg


def test_process_claims_rpc_hint_on_dns_error(monkeypatch, mock_setup):
    env, mock_near = mock_setup

    # Enable signing; intentionally mis-set network to see hint
    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setenv("NEAR_NETWORK", "mainnet")

    def raise_dns_error(*args, **kwargs):
        raise RuntimeError("getaddrinfo ENOTFOUND rpc")

    mock_near.call = AsyncMock(side_effect=raise_dns_error)

    pc.process_claims("vault-dns.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "RPC appears unreachable" in msg
    assert "NEAR_NETWORK" in msg

