# Quickstart: Cloud-Native Scheduled Operation on Azure

**Feature**: `001-azure-deployment` | **Date**: 2026-06-11

Validation/run guide. Resource and parameter details live in
[contracts/deployment.md](contracts/deployment.md); env vars, exit codes, and log
markers in [contracts/runtime-config.md](contracts/runtime-config.md).

## Prerequisites

- Azure subscription + tenant ID (supplied at deploy time, never committed)
- `az` CLI logged in as the maintainer; Docker; `uv`; `gh` (optional)
- GitHub repo admin (to confirm Actions + OIDC)
- Local copies of `profile.md`, `screening_prompt.md`, `registry.txt` (git-ignored)
- SMTP credentials and the Anthropic API key at hand (values never written down
  anywhere in the repo)

## Bootstrap (once per environment; target ≤1 h per SC-006)

1. `scripts/bootstrap.sh` — resource group + GitHub-OIDC deploy identity.
2. `az deployment group create -g <rg> -f infra/main.bicep -p infra/main.bicepparam`
   (supply `smsCountryCode`/`smsPhone` and `alertEmail` as CLI parameters).
3. Set the six Key Vault secrets (names in
   [deployment.md](contracts/deployment.md) → Bootstrap contract).
4. Upload the three runtime files with `az storage file upload` (FR-012 mechanism).
5. Set the Anthropic Console spend limit (suggested $25/mo).
6. Push to `main` (or re-run the workflow) so CI publishes the image and points the
   job at it.

Known cosmetic caveat: the absence-of-success alert fires until the first
successful run completes (research §8).

## Validation per user story

### US1 — Morning digest arrives unattended

- After bootstrap, wait for the next scheduled window (11:00–11:40 UTC) with all
  local machines off. Confirm the digest (or no-matches notice) is in the inbox by
  06:00 America/Los_Angeles.
- Empty day: with no new qualifying postings, confirm the "no new matches" notice
  still arrives (FR-003 / SC-007).
- No duplicates: confirm a posting from yesterday's digest is absent from today's.
- Logs show `RUN_SUCCESS digest_date=<today>`:
  `az containerapp job logs show -n <job> -g <rg>` or LAW query on
  `ContainerAppConsoleLogs_CL`.

### US2 — Push to main ships to production

- Green path: push a trivially observable change; confirm Actions runs
  test → build → deploy and the change is live within 15 min (SC-003) with no
  further action.
- Red path: push a deliberately failing test on a throwaway commit; confirm deploy
  is blocked and the previous image keeps running; revert.
- Inspect the Actions logs for the run: no secret values anywhere (FR-011).

### US3 — Failures are visible by morning coffee

- On-demand run (recovery path):
  `az containerapp job start -n <job> -g <rg>` — confirm it no-ops with exit 0 if
  today already succeeded, and runs fully with `--env-vars JOBAGENT_FORCE=1`.
- Alert drill (controlled): temporarily break a secret (e.g. rename
  `smtp-host` in Key Vault) or let a night pass with the job disabled; confirm
  **both** the alert email and the SMS arrive by ~06:30 (SC-004), then restore and
  force a run.
- Degraded source: add a bogus slug to `registry.txt` (via the upload mechanism),
  force a run, confirm the digest arrives and names the failed source (FR-005);
  remove it after.
- Diagnosis: for the drill failures above, confirm the cause is identifiable from
  retained logs alone (SC-005).

### US4 — Updating personal files in production

- Edit `profile.md` locally with an observable criteria change; upload via
  `az storage file upload`; force a run; confirm the scoring rationale reflects the
  change.
- Confirm the storage account refuses anonymous/public access and the files are
  absent from the repo and the image.

### US5 — Rebuild everything from the repository

- In a fresh resource group, repeat Bootstrap start-to-finish using only repo
  contents + secrets + runtime files; time it (≤1 h, SC-006); confirm the next
  scheduled run delivers end-to-end, with no portal-only steps (FR-010).
- Drift check: re-run the `az deployment group create` from step 2 unchanged;
  confirm it is a no-op that restores/declares the same state.

## Local development unchanged

```bash
uv sync
uv run pytest               # suite green, no network
uv run python main.py      # full pipeline against local jobs.db (JOBAGENT_DATA_DIR default ".")
```
