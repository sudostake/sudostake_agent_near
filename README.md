# SudoStake Agent (NEAR)

Typed Python agent to inspect and manage SudoStake vaults on NEAR. Runs locally in view‑only mode by default and can sign transactions when you provide keys.

The agent must be built once before interactive use. The build copies code into your local NEAR‑AI registry. After building, simply run `nearai agent interactive --local` (the CLI defaults to mainnet). Switch networks via your profile or set `NEAR_NETWORK` inline.

## Requirements
- Python 3.9+ and `pip`
- Git
- `jq` (for the build script)
- Python package `semver` (for version bumping)
- macOS/Linux shell (Bash)

## Quick Start
1) Clone and create a virtualenv
   - `git clone <your-fork-or-repo-url>`
   - `cd sudostake_agent_near`
   - `python3 -m venv .venv && source .venv/bin/activate`
2) Install dependencies (includes the `nearai` CLI)
   - `pip install -r requirements.txt`
3) Install build prerequisites
   - `pip install semver`
   - macOS: `brew install jq`  •  Debian/Ubuntu: `sudo apt install -y jq`
4) Build the agent (copies into `~/.nearai/registry/...`)
   - `chmod +x agent/build.sh && ./agent/build.sh patch`
5) Run
   - View‑only: `nearai agent interactive --local`
   - With signing: `source ~/.near_agent_profile && nearai agent interactive --local`
   - Testnet inline override: `NEAR_NETWORK=testnet nearai agent interactive --local`

Tip: In the REPL, type `help` to see available commands.

## Signing Profile (optional)
Create a single profile with your credentials and preferred network.

Profile template (`~/.near_agent_profile`)
```
# ~/.near_agent_profile
export NEAR_NETWORK=mainnet            # or testnet
export NEAR_ACCOUNT_ID=<your-account>
export NEAR_PRIVATE_KEY=<ed25519:...>
```
Usage: `source ~/.near_agent_profile && nearai agent interactive --local`

## Developer Loop
- Run tests: `pytest -q`
- Edit code under `agent/src`
- Rebuild: `./agent/build.sh patch` (or `minor` | `major`)
- Run: `nearai agent interactive --local` (uses the latest build)

## Docs Vector Store (optional)
- Add `.md` files under `agent/docs/`
- Ensure `nearai` CLI is configured
- Build: `python agent/jobs/init_vector_store_job.py`

## Project Structure
- `agent/src` — entrypoint (`agent.py`), helpers, tools
- `agent/tests` — pytest suite
- `agent/jobs` — ops jobs (e.g., vector store)
- `agent/build.sh` — build and versioning
- `agent/metadata.json` — stamped during build

## Troubleshooting
- Initialization/network
  - `nearai` defaults to mainnet; use a profile or inline `NEAR_NETWORK` to switch.
- CLI not found
  - Activate venv and reinstall deps: `source .venv/bin/activate && pip install -r requirements.txt`.
- Build errors (jq/semver)
  - `pip install semver` and install `jq` via your package manager.
- No docs found when building vector store
  - Add Markdown files under `agent/docs/` first.

## Security
- Keep `NEAR_PRIVATE_KEY` secret; never commit it.
- Prefer profile files and local shell secret management.
