from unittest.mock import AsyncMock, MagicMock
from tools import active_loan
import helpers
import json


def event_json(event, data=None):
    payload = {"event": event}
    if data is not None:
        payload["data"] = data
    return f"EVENT_JSON:{json.dumps(payload)}"


def test_repay_loan_success(monkeypatch, mock_setup):
    """Should repay loan successfully and confirm to user."""

    env, mock_near = mock_setup

    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx987"),
        logs=[],
        status={"SuccessValue": ""},
    ))

    mock_index = MagicMock()
    monkeypatch.setenv("NEAR_NETWORK", "testnet")
    monkeypatch.setattr(active_loan, "index_vault_to_firebase", mock_index)

    active_loan.repay_loan("vault-0.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Loan Repaid Successfully" in msg
    assert "vault-0.factory.testnet" in msg
    assert "tx987" in msg

    mock_index.assert_called_once_with("vault-0.factory.testnet", "tx987")


def test_repay_loan_contract_panic(monkeypatch, mock_setup):
    """Should detect contract panic and return a failure message."""

    env, mock_near = mock_setup

    # Simulate contract-level panic in transaction status
    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_panic"),
        logs=[],
        status={
            "Failure": {
                "ActionError": {
                    "kind": {
                        "FunctionCallError": {
                            "ExecutionError": "Smart contract panicked: Loan already repaid"
                        }
                    }
                }
            }
        }
    ))

    # Run the tool
    active_loan.repay_loan("vault-0.factory.testnet")

    # Assert proper failure message is returned
    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]

    assert "Loan repayment failed" in msg
    assert "contract panic" in msg
    assert "Loan already repaid" in msg


def test_repay_loan_ft_transfer_failure_log(monkeypatch, mock_setup):
    """Should detect FT transfer failure from logs and return a user-friendly error."""

    env, mock_near = mock_setup

    # Simulate successful status but log indicates transfer failure
    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_logs"),
        logs=[
            event_json("repay_loan_failed")
        ],
        status={"SuccessValue": ""},
    ))

    # Run the tool
    active_loan.repay_loan("vault-0.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]

    assert "Loan repayment failed" in msg
    assert "could not be transferred" in msg.lower()


def test_repay_loan_indexing_failure(monkeypatch, mock_setup):
    """Should still return success even if Firebase indexing fails."""

    env, mock_near = mock_setup

    # Simulate successful NEAR call
    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx456"),
        logs=[],
        status={"SuccessValue": ""},
    ))

    # Simulate failure in Firebase indexer
    def raise_index_error(vault_id, tx_hash):
        raise Exception("Firestore write failed")

    monkeypatch.setattr(active_loan, "index_vault_to_firebase", raise_index_error)
    monkeypatch.setenv("NEAR_NETWORK", "testnet")

    active_loan.repay_loan("vault-4.factory.testnet")

    # Check that user still receives a success message
    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]

    assert "Loan Repaid Successfully" in msg
    assert "vault-4.factory.testnet" in msg
    assert "tx456" in msg


def test_repay_loan_runtime_crash(monkeypatch, mock_setup):
    """Should handle unexpected exception from near.call gracefully."""

    env, mock_near = mock_setup

    def raise_runtime_error(*args, **kwargs):
        raise RuntimeError("network dropped")

    mock_near.call = AsyncMock(side_effect=raise_runtime_error)

    active_loan.repay_loan("vault-99.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]

    assert "Unexpected error" in msg
    assert "network dropped" in msg


# ───────────────── process_claims tests ─────────────────
def test_process_claims_no_credentials(monkeypatch, mock_setup):
    """Should warn and return when no signing keys are available."""

    env, _ = mock_setup

    # Ensure signing is disabled
    monkeypatch.setattr(helpers, "_signing_mode", None, raising=False)

    active_loan.process_claims("vault-xyz.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "No signing keys" in msg


def test_process_claims_not_allowed_until_timestamp(monkeypatch, mock_setup):
    """Should parse 'not allowed until <ts>' and format a friendly time."""

    env, mock_near = mock_setup

    # Enable headless mode
    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setenv("NEAR_NETWORK", "testnet")

    # Mock chain response with a panic containing the expected phrase
    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_na"),
        logs=[],
        status={
            "Failure": {
                "ActionError": {
                    "kind": {
                        "FunctionCallError": {
                            "ExecutionError": "Liquidation not allowed until 1700000000000000000"
                        }
                    }
                }
            }
        }
    ))

    # Make timestamp formatting deterministic
    monkeypatch.setattr(active_loan, "format_near_timestamp", lambda ns: "2024-01-02 03:04 UTC")

    active_loan.process_claims("vault-7.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Liquidation not allowed yet" in msg
    assert "2024-01-02 03:04 UTC" in msg
    assert "vault-7.factory.testnet" in msg


def test_process_claims_contract_panic_generic(monkeypatch, mock_setup):
    """Should show a generic contract panic when no specific mapping applies."""

    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setenv("NEAR_NETWORK", "testnet")

    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_panic2"),
        logs=[],
        status={
            "Failure": {
                "ActionError": {
                    "kind": {
                        "FunctionCallError": {
                            "ExecutionError": "Some other error"
                        }
                    }
                }
            }
        }
    ))

    active_loan.process_claims("vault-a.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Processing claims failed" in msg
    assert "contract panic" in msg


def test_process_claims_completed_success(monkeypatch, mock_setup):
    """Should detect completion and return a success message including explorer links."""

    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setenv("NEAR_NETWORK", "testnet")

    logs = ["liquidation_complete"]
    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_done"),
        logs=logs,
        status={"SuccessValue": ""},
    ))

    mock_index = MagicMock()
    monkeypatch.setattr(active_loan, "index_vault_to_firebase", mock_index)

    active_loan.process_claims("vault-done.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Liquidation Complete" in msg
    assert "vault-done.factory.testnet" in msg
    assert "tx_done" in msg
    mock_index.assert_called_once_with("vault-done.factory.testnet", "tx_done")


def test_process_claims_progress_with_details(monkeypatch, mock_setup):
    """Should report progress details, including reason and unstake specifics."""

    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setenv("NEAR_NETWORK", "testnet")

    logs = [
        "liquidation_started",
        event_json("unstake_recorded", {
            "validator": "val.poolv1.near",
            "amount": "1230000000000000000000000",
            "epoch_height": 1234,
        }),
        event_json("liquidation_progress", {"reason": "awaiting unstake"}),
        "unstake_failed",
    ]

    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_prog"),
        logs=logs,
        status={"SuccessValue": ""},
    ))

    active_loan.process_claims("vault-prog.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Claims Processing In Progress" in msg
    assert "Liquidation started" in msg
    assert "Unstake recorded" in msg
    assert "val.poolv1.near" in msg
    assert "epoch" in msg
    assert "Waiting for available/matured" in msg
    assert "unstake attempt failed" in msg
    assert "Reason: awaiting unstake" in msg


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

    active_loan.process_claims("vault-g.factory.testnet")

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

    active_loan.process_claims("vault-idx.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Liquidation Complete" in msg
    assert "tx_idx" in msg


def test_process_claims_runtime_error(monkeypatch, mock_setup):
    """Unexpected exceptions from near.call should be handled gracefully."""

    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)

    mock_near.call = AsyncMock(side_effect=RuntimeError("boom"))

    active_loan.process_claims("vault-err.factory.testnet")

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

    active_loan.process_claims("vault-busy.factory.testnet")

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

    active_loan.process_claims("vault-busy2.factory.testnet")

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

    active_loan.process_claims("vault-yocto.factory.testnet")

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

    active_loan.process_claims("vault-nooffer.factory.testnet")

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

    active_loan.process_claims("vault-done-json.factory.testnet")

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

    active_loan.process_claims("vault-start.factory.testnet")

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

    active_loan.process_claims("vault-uf.factory.testnet")

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

    active_loan.process_claims("vault-prog2.factory.testnet")

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

    active_loan.process_claims("vault-dns.factory.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "RPC appears unreachable" in msg
    assert "NEAR_NETWORK" in msg
