# SudoStake Agent NEAR


## Activate python virtual environment
```
python3 -m venv .venv
source .venv/bin/activate
```

&nbsp;

## Test SudoStake AI agent
```
pip install -r requirements.txt
pytest -v
```

&nbsp;

## Build SudoStake AI agent
```
pip install semver
brew install jq  # macOS  (or: sudo apt install jq on Debian/Ubuntu)

chmod +x ./agent/build.sh

./agent/build.sh patch
```

&nbsp;

## Run the agent locally in interactive mode
```
# Interact as a vault owner
source ~/.near_vault_owner_profile && nearai agent interactive --local

# Interact as a usdc lender
source ~/.near_vault_lender_profile && nearai agent interactive --local
```

&nbsp;

## Required environment variables
```
export NEAR_NETWORK=<testnet>
export NEAR_ACCOUNT_ID=<account.testnet>
export NEAR_PRIVATE_KEY=<ed25519:(key)>
```