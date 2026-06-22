# Quickstart: Cloud-Native Scheduled Operation on Azure

**Feature**: `001-azure-deployment` | **Date**: 2026-06-11

**Maintainer acceptance validation.** This guide is the per-user-story check that
confirms the Azure deployment behaves correctly. It requires Azure access and is
**not** part of forker setup — to stand up your own instance, follow
[docs/manual-deployment.md](../../docs/manual-deployment.md) (manual deploy) or
[docs/ci-cd.md](../../docs/ci-cd.md) (CI), then come here only to reproduce the
acceptance checks. Resource and parameter details live in
[contracts/deployment.md](contracts/deployment.md); env vars, exit codes, and log
markers in [contracts/runtime-config.md](contracts/runtime-config.md).

## Validation per user story

### US1 — Morning digest arrives unattended

- After deploy, wait for the next scheduled window (11:00–11:40 UTC) with all
  local machines off. Confirm the digest (or no-matches notice) is in the inbox by
  06:00 America/Los_Angeles.
- Empty day: with no new qualifying postings, confirm the "no new matches" notice
  still arrives (FR-003 / SC-007).
- No duplicates: confirm a posting from yesterday's digest is absent from today's.
- Logs show `RUN_SUCCESS digest_date=<today>`:
  `az containerapp job logs show -n <job> -g <rg> --container job-agent` or LAW
  query on `ContainerAppConsoleLogs_CL`.

### US2 — Push to main ships to production

- Green path: push a trivially observable change; confirm Actions runs
  test → build → deploy and the change is live within 15 min (SC-003) with no
  further action.
- Red path: push a deliberately failing test on a throwaway commit; confirm deploy
  is blocked and the previous image keeps running; revert.
- Inspect the Actions logs for the run: no secret values anywhere (FR-011).

### US3 — Failures are visible by morning coffee

- On-demand run (recovery path): `az containerapp job start -n <job> -g <rg>` —
  confirm it no-ops with exit 0 if today already succeeded. To force a full re-run
  after a same-day success, use the merge-safe update/start/cleanup sequence in
  [docs/manual-deployment.md](../../docs/manual-deployment.md) ("Forcing a re-run
  after a same-day success").
- Alert drill (controlled): park the schedule for one night so no run records a
  success — `az containerapp job update -n <job> -g <rg> --cron-expression
  "0 0 31 2 *"` (a date that never occurs). Past the local deadline with no
  `RUN_SUCCESS`, confirm **both** the alert email and the SMS arrive by ~06:30
  (SC-004). Restore the real schedule (`--cron-expression "0,20,40 11 * * *"`) and
  force a clean run so autoMitigate resolves the alert.
- Degraded source: add a bogus slug to `registry.txt` (via the upload mechanism),
  force a run, confirm the digest arrives and names the failed source (FR-005);
  remove it after.
- Diagnosis: confirm the degraded-source failure cause is identifiable from
  retained logs alone (SC-005).

### US4 — Updating personal files in production

- Edit `profile.md` locally with an observable criteria change; upload via
  `az storage file upload`; force a run; confirm the scoring rationale reflects the
  change.
- Confirm the storage account refuses anonymous/public access and the files are
  absent from the repo and the image.

### US5 — Rebuild everything from the repository

- In a fresh resource group, repeat
  [docs/manual-deployment.md](../../docs/manual-deployment.md) start-to-finish
  using only repo contents + secrets + runtime files; time it (≤1 h, SC-006);
  confirm the next scheduled run delivers end-to-end, with no portal-only steps
  (FR-010).
- Drift check: re-run the `az deployment group create` from the deploy step
  unchanged; confirm it is a no-op that restores/declares the same state.
