from unittest.mock import AsyncMock, MagicMock
from tools import active_loan


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
