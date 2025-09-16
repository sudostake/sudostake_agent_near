from unittest.mock import AsyncMock, MagicMock
from tools import active_loan
import helpers


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
            'EVENT_JSON:{"event":"repay_loan_failed"}'
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
        'EVENT_JSON:{"event":"unstake_recorded","data":{"validator":"val.poolv1.near","amount":"1230000000000000000000000","epoch_height": 1234}}',
        'EVENT_JSON:{"event":"liquidation_progress","data":{"reason":"awaiting unstake"}}',
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
