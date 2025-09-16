# SudoStake Agent (NEAR)

Typed Python agent to inspect and manage SudoStake vaults on NEAR. Runs locally in read‑only mode by default; can sign transactions when you provide keys. Tests are fast, and releases build from a single script.

## Prerequisites
- Python 3.9+ and `pip`
- Optional (to run interactively): NEAR AI CLI `nearai`
- For building releases: `jq` and Python `semver` package

## Setup
1) Create and activate a virtual environment
   - `python3 -m venv .venv`
   - `source .venv/bin/activate`
2) Install dependencies
   - `pip install -r requirements.txt`
3) Run tests (optional, recommended)
   - `pytest -q`

## Run (interactive)
The agent works without keys (view‑only) or with keys (can sign actions like delegate, mint, withdraw).

- View‑only: `nearai agent interactive --local`
- With a profile (recommended):
  - As a vault owner: `source ~/.near_vault_owner_profile && nearai agent interactive --local`
  - As a USDC lender: `source ~/.near_vault_lender_profile && nearai agent interactive --local`

If you don’t have `nearai`, install it per NEAR AI docs or use the environment variables below to run headless.

## Enable Signing (headless)
Set these environment variables to allow the agent to sign transactions. If unset, the agent stays view‑only.

```
export NEAR_NETWORK=testnet            # or mainnet
export NEAR_ACCOUNT_ID=<account.testnet>
export NEAR_PRIVATE_KEY=<ed25519:...>
```

### Example profiles (replace with your values)
Owner (`~/.near_vault_owner_profile`):
```
export NEAR_NETWORK=testnet
export NEAR_ACCOUNT_ID=owner.demo.testnet
export NEAR_PRIVATE_KEY=ed25519:1111111111111111111111111111111111111111111111111111111111111111
```

Lender (`~/.near_vault_lender_profile`):
```
export NEAR_NETWORK=testnet
export NEAR_ACCOUNT_ID=lender.demo.testnet
export NEAR_PRIVATE_KEY=ed25519:2222222222222222222222222222222222222222222222222222222222222222
```

## Build From Source
Build a versioned artifact of the agent that you can run locally or upload to the NEAR AI registry.

Prerequisites
- `pip install semver`
- macOS: `brew install jq`  • Debian/Ubuntu: `sudo apt install jq`

Steps
1) Make the build script executable
   - `chmod +x ./agent/build.sh`
2) Build with a version bump
   - `./agent/build.sh patch`  (or `minor` | `major`)
3) Note the output folder
   - The script prints the destination like `~/.nearai/registry/sudostake.near/sudo/1.2.3`
   - It copies `agent/src` into that folder and stamps `metadata.json`

Run the built artifact locally
- `nearai agent interactive "~/.nearai/registry/sudostake.near/sudo/1.2.3" --local`

Upload to registry (optional)
- `nearai registry upload "~/.nearai/registry/sudostake.near/sudo/1.2.3"`

## Project Structure
- `agent/src` — Agent code
  - `agent/src/tools` — Vault, delegation, liquidity, etc.
  - `agent/src/agent.py` — Entry point for NEAR AI runtime
  - `agent/src/helpers.py` — Env, constants, formatting
- `agent/tests` — Pytest suite (fast, isolated)
- `agent/jobs` — Optional maintenance jobs (e.g., vector store init)

## Troubleshooting
- Editor imports: `pyrightconfig.json` sets `extraPaths` for `agent/src` and `agent/jobs`.
- SSL warnings in tests: urllib3/LibreSSL warnings are harmless locally.
