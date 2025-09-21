from unittest.mock import AsyncMock, MagicMock
import helpers
from decimal import Decimal

from tools import token_registration
from test_utils import failure_exec_error


def test_register_requires_headless(monkeypatch, mock_setup):
    env, _ = mock_setup

    # Not headless → should show keys hint and return
    monkeypatch.setattr(helpers, "_signing_mode", "interactive", raising=False)

    token_registration.register_account_with_token("me")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "No signing keys available" in msg


def test_register_missing_account_id_when_me(monkeypatch, mock_setup):
    env, _ = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setattr(token_registration, "account_id", lambda: None)

    token_registration.register_account_with_token("me")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "No account ID available" in msg


def test_register_already_registered(monkeypatch, mock_setup):
    env, _ = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setattr(token_registration, "_storage_balance_of", lambda t, a: {"total": "1"})

    token_registration.register_account_with_token("vault-xyz.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "already registered" in msg


def test_register_success_with_bounds(monkeypatch, mock_setup):
    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setenv("NEAR_NETWORK", "testnet")
    # Not registered path
    monkeypatch.setattr(token_registration, "_storage_balance_of", lambda t, a: None)
    # storage_balance_bounds has a specific min
    mock_near.view = AsyncMock(return_value=MagicMock(result={"min": "100"}))
    # Simulate successful tx
    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_ok"),
        status={"SuccessValue": ""},
    ))

    token_registration.register_account_with_token("alice.testnet")

    # Verify we attached the min deposit from bounds
    assert mock_near.call.call_args.kwargs["amount"] == 100
    env.add_reply.assert_called()
    msg = env.add_reply.call_args[0][0]
    assert "Registered Account With Token" in msg
    assert "tx_ok" in msg


def test_register_success_with_fallback_deposit(monkeypatch, mock_setup):
    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setenv("NEAR_NETWORK", "testnet")
    monkeypatch.setattr(token_registration, "_storage_balance_of", lambda t, a: None)
    # storage_balance_bounds missing/invalid → fallback deposit
    mock_near.view = AsyncMock(return_value=MagicMock(result=None))
    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_fallback"),
        status={"SuccessValue": ""},
    ))

    token_registration.register_account_with_token("vault-123.testnet")

    fallback = int((Decimal("0.00125") * helpers.YOCTO_FACTOR).quantize(Decimal("1")))
    assert mock_near.call.call_args.kwargs["amount"] == fallback
    env.add_reply.assert_called()
    msg = env.add_reply.call_args[0][0]
    assert "Registered Account With Token" in msg
    assert "tx_fallback" in msg


def test_register_tx_failure(monkeypatch, mock_setup):
    env, mock_near = mock_setup

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setattr(token_registration, "_storage_balance_of", lambda t, a: None)
    mock_near.view = AsyncMock(return_value=MagicMock(result={"min": "200"}))
    mock_near.call = AsyncMock(return_value=MagicMock(
        transaction=MagicMock(hash="tx_fail"),
        status=failure_exec_error("Some storage_deposit error"),
    ))

    token_registration.register_account_with_token("vault-abc.testnet")

    env.add_reply.assert_called_once()
    msg = env.add_reply.call_args[0][0]
    assert "Failed to register account with token" in msg
    assert "Some storage_deposit error" in msg


def test_wrapper_forwarding(monkeypatch, mock_setup):
    env, _ = mock_setup

    called = []
    def spy(acct):
        called.append(acct)

    monkeypatch.setattr(helpers, "_signing_mode", "headless", raising=False)
    monkeypatch.setattr(token_registration, "register_account_with_token", spy)

    token_registration.register_vault_with_token("vault-9.factory.testnet")
    token_registration.register_me_with_token()

    assert "vault-9.factory.testnet" in called
    assert "me" in called
