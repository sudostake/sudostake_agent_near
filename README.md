# SudoStake Agent (NEAR)

Simple, typed Python agent that helps you inspect and manage SudoStake vaults on NEAR. You can run it locally, test it quickly, and ship new builds with a single script.

## Quickstart
- Requirements: Python 3.9+, pip, and optionally the NEAR AI CLI (`nearai`) if you want to run interactively.

1) Create a virtualenv and install deps
   - `python3 -m venv .venv`
   - `source .venv/bin/activate`
   - `pip install -r requirements.txt`

2) Run tests
   - `pytest -q`

3) Build (optional)
   - `./agent/build.sh patch`  (use `minor` or `major` to bump accordingly)
   - Prereqs: `pip install semver`, and `jq` installed (`brew install jq` on macOS or `sudo apt install jq` on Debian/Ubuntu)

4) Run the agent locally (interactive)
   - As a vault owner: `source ~/.near_vault_owner_profile && nearai agent interactive --local`
   - As a USDC lender: `source ~/.near_vault_lender_profile && nearai agent interactive --local`

Notes
- Without signing keys, the agent runs in read‑only mode and still answers queries (e.g., docs, views).
- With signing keys, it can sign transactions (delegate, mint, withdraw, etc.).

## Environment Variables (headless signing)
Set these if you want the agent to sign transactions without a wallet prompt:

```
export NEAR_NETWORK=testnet            # or mainnet
export NEAR_ACCOUNT_ID=<account.testnet>
export NEAR_PRIVATE_KEY=<ed25519:...>
```

If these are not set, the agent remains view‑only.

### Example Profiles (dummy data)
Save these files to your shell and source them before running interactively. Replace the dummy values with your own.

Owner profile (`~/.near_vault_owner_profile`):
```
export NEAR_NETWORK=testnet
export NEAR_ACCOUNT_ID=owner.demo.testnet
export NEAR_PRIVATE_KEY=ed25519:1111111111111111111111111111111111111111111111111111111111111111
```

Lender profile (`~/.near_vault_lender_profile`):
```
export NEAR_NETWORK=testnet
export NEAR_ACCOUNT_ID=lender.demo.testnet
export NEAR_PRIVATE_KEY=ed25519:2222222222222222222222222222222222222222222222222222222222222222
```

## Build a Release
The build script bumps a version and prepares an artifact.

Prereqs:
- `pip install semver`
- macOS: `brew install jq`  (Debian/Ubuntu: `sudo apt install jq`)

Run:
```
chmod +x ./agent/build.sh
./agent/build.sh patch     # or: minor | major
```

## Project Structure
- `agent/src` — Agent code
  - `agent/src/tools` — Domain tools (vault, delegation, liquidity, etc.)
  - `agent/src/agent.py` — Entry point wired to NEAR AI runtime
  - `agent/src/helpers.py` — Shared helpers (env, constants, formatting)
- `agent/tests` — Pytest suite (fast, isolated)
- `agent/jobs` — Optional maintenance jobs (e.g., vector store init)

## Common Tasks
- Run tests: `pytest -q`
- Run interactively: `nearai agent interactive --local` (with a sourced profile)
- Lint/type in your IDE: repo includes `pyrightconfig.json` and `.editorconfig`

## Troubleshooting
- Pylance import resolution: the repo includes `pyrightconfig.json` with `extraPaths` so editors can resolve `agent/src` and `agent/jobs`.
- SSL warning in tests: urllib3 may warn about LibreSSL; it’s harmless for local tests.
