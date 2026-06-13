# Tasks: Cloud-Native Scheduled Operation on Azure

**Input**: Design documents from `/specs/001-azure-deployment/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/runtime-config.md](contracts/runtime-config.md),
[contracts/deployment.md](contracts/deployment.md), [quickstart.md](quickstart.md)

**Tests**: Included — the constitution makes TDD NON-NEGOTIABLE (Principle III): failing
tests are written and observed red before implementation, and `uv run pytest` must pass
before any task group is called done. Tests are stub-based with no network calls,
following existing `tests/` patterns.

**Organization**: Tasks are grouped by user story (US1–US5 from spec.md) so each story
is independently implementable and testable. Cross-cutting requirements with no owning
story (retention purge FR-015, budget FR-014, log-content audit FR-007) land in the
final Polish phase.

**⚠️ Privacy (Constitution VI)**: `profile.md`, `screening_prompt.md`, `registry.txt`,
and `jobs.db` are git-ignored personal data. No task may commit them, bake them into
the image, or quote their contents in any output. Validation tasks that touch them run
against the maintainer's private copies only.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story the task belongs to (US1–US5); Setup/Foundational/Polish tasks carry no story label
- Every task names exact file paths

---

## Reconciliation note (2026-06-13, post-Feature-002)

Feature 002 (scoring spend efficiency, shipped 0.1.1) landed between US1 and the rest of
this feature and shifted some ground truth. Corrections:

- **US1 / T024**: the MVP is **built and live in Azure** (`jobagent-rg`, 0.1.1 image) and
  delivering. T024 stays open only for the *formal* SC-001 evidence — notably the alert
  drill, which cannot run until US3 alerting exists.
- **T029 / T032 — SUPERSEDED**: the planned `JOBAGENT_MAX_LLM_CALLS` cap was replaced by
  Feature 002's `JOBAGENT_MAX_POSTINGS_PER_RUN` + `JOBAGENT_MAX_COST_PER_RUN` (+ price vars).
  FR-013 (per-run call bound) is **met**; the still-open part is the **FR-020 digest
  degradation report**, which remains under US3 (T033/T034).
- **T022 / T023**: `infra/main.bicep` + `main.bicepparam` already carry the Feature-002 env
  vars (no `MAX_LLM_CALLS`) — effectively done.
- **T025 (deploy.yml)**: ships **imageTag-only**. The alert params
  (`alertEmail`/`smsCountryCode`/`smsPhone`) are **deferred to US3** — Bicep has no such
  params until T036; a marker in the workflow records the follow-up.
- **T041 / T042 — NOT implemented**: `JOBAGENT_RETENTION_DAYS` is wired into Bicep, but no
  retention-purge code exists in `store.py`/`main.py` yet. Still open (Polish).

Accompanies `docs/work/us2-cicd/plan.md`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Containerization artifacts both the scheduled job (US1) and CI (US2) need.

- [x] T001 Create `Dockerfile` at repo root: uv-based Python ≥3.12 image that installs the project (hatchling build, `anthropic` + `requests` only runtime deps) and runs `python main.py` as CMD, per research.md §12.7 and plan.md structure
- [x] T002 [P] Create `.dockerignore` at repo root excluding personal/runtime data and non-runtime files (`profile.md`, `screening_prompt.md`, `registry.txt`, `jobs.db`, `.git`, `tests/`, `specs/`, `docs/`) so the public image contains only public-repo code (FR-021, Constitution VI)
- [x] T003 Verify `docker build .` succeeds locally and the image entrypoint invokes `python main.py` (fails loud on missing config rather than silently exiting) — depends on T001, T002

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Data-dir configurability, the dedupe-identity revision + migration, the
model-default bump (current default retires 2026-06-15), and the bootstrap script —
groundwork every story builds on.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

### Tests (write first, observe red)

- [x] T004 [P] Write failing tests in `tests/test_schema.py`: `Posting.fingerprint` is `sha256(title|company|location|description, lowercased)[:16]` — description is a fourth key component; two same-title/company/location postings with different descriptions get distinct fingerprints; identical cross-board re-posts still collapse (data-model.md "Dedupe identity revision")
- [x] T005 [P] Write failing tests in `tests/test_store.py`: (a) `JOBAGENT_DATA_DIR` prefixes the default `jobs.db` path while default `.` preserves current behavior; (b) `_migrate` recomputes every existing fingerprint from stored columns and rewrites `postings.fingerprint` + matching `applications.fingerprint` in one transaction, preserving scores, statuses, and `digest_sent_at` (data-model.md "Migration")
- [x] T006 [P] Write failing tests in `tests/test_fetch.py`: `load_registry` resolves `registry.txt` under `JOBAGENT_DATA_DIR` (default `.` unchanged)
- [x] T007 [P] Write failing tests in new `tests/test_score.py`: (a) `score` resolves `profile.md` and `screening_prompt.md` under `JOBAGENT_DATA_DIR`; (b) default model is `claude-sonnet-4-6` when `JOBAGENT_MODEL` is unset (research.md §11)

### Implementation

- [x] T008 Revise `Posting.fingerprint` in `src/job_agent/schema.py` to include the cleaned description as the fourth key component, updating the docstring to record the accepted re-surfacing failure mode (data-model.md) — makes T004 green
- [x] T009 In `src/job_agent/store.py`: add a data-dir path helper (env `JOBAGENT_DATA_DIR`, default `.`) applied to the default db path; add the fingerprint re-key migration to `_migrate` (single transaction, recompute from stored columns); extend the `applications.status` comment to include `duplicate` (data-model.md) — makes T005 green; depends on T008
- [x] T010 [P] Apply the data-dir helper to the `registry.txt` path in `src/job_agent/fetch.py` — makes T006 green; depends on T009
- [x] T011 [P] In `src/job_agent/score.py`: apply the data-dir helper to `profile.md`/`screening_prompt.md` reads and bump the default model to `claude-sonnet-4-6` — makes T007 green; depends on T009
- [x] T012 Run `uv run pytest` — entire suite green before any user story begins
- [x] T013 [P] Create `scripts/bootstrap.sh` (run once per environment): create resource group; create user-assigned managed identity; add federated credential trusting `repo:<owner>/job-agent:ref:refs/heads/main`; grant Contributor + role-assignment rights scoped to the resource group; subscription/tenant IDs and repo slug are inputs, never committed (contracts/deployment.md "Bootstrap contract")

**Checkpoint**: Foundation ready — user story phases can begin.

---

## Phase 3: User Story 1 - Morning digest arrives unattended (Priority: P1) 🎯 MVP

**Goal**: The unchanged pipeline runs as a scheduled Azure Container Apps Job; an email
(digest or no-matches notice) is in the inbox by 06:00 America/Los_Angeles every day
with no local machine involved; sent-state survives runs; three spaced cron ticks plus
the `runs`-table idempotency check implement the retry policy.

**Independent Test**: Deploy against a small registry, power off local machines, and
confirm the digest (or no-matches notice) arrives by 06:00 Pacific after the scheduled
window; a posting from yesterday's digest is not repeated; an empty day still produces
an email (quickstart.md §US1).

### Tests for User Story 1 (write first, observe red)

- [x] T014 [P] [US1] Write failing tests in `tests/test_store.py` for the `runs` table: DDL columns per data-model.md; helpers to start a run (1-based `attempt` per `digest_date`), finish with outcome `success`/`degraded`/`failed`, and evaluate the startup check — in-flight NULL-outcome row within ~900 s blocks (not bypassed by `JOBAGENT_FORCE`), stale NULL-outcome row is marked `failed` and proceeds, `success`/`degraded` row for the date skips unless `JOBAGENT_FORCE=1`; plus timezone-aware `digest_date` computation via `JOBAGENT_TZ` (stdlib `zoneinfo`, default `America/Los_Angeles`)
- [x] T015 [P] [US1] Write failing tests in new `tests/test_main.py` (stages monkeypatched, no network): no-op skip paths exit 0; `RUN_SUCCESS digest_date=<YYYY-MM-DD>` printed only after a confirmed send (and on no-op skips of an already-successful date); `RUN_FAILED_FINAL digest_date=<…>` printed on a fatal failure when `attempt` ≥ 3 (the day's last scheduled tick); fatal stage failure records run outcome `failed` and exits non-zero (contracts/runtime-config.md exit-code + log-marker contracts)
- [x] T016 [P] [US1] Write failing tests in `tests/test_digest.py`: when no rows qualify, `digest.main` sends a "no new matches" notice email instead of returning silently (FR-003); the send path reports confirmed-send status to its caller so success state can be committed strictly after the send
- [x] T017 [US1] Implement the `runs` table DDL, run-lifecycle helpers, startup check, and `digest_date()` (zoneinfo) in `src/job_agent/store.py` — makes T014 green

### Implementation for User Story 1

- [x] T018 [US1] Implement the empty-day notice in `src/job_agent/digest.py`: send the no-matches email through the existing SMTP path, keep `DIGEST_DRY_RUN`, and return confirmed-send status — makes T016 green; depends on T017
- [x] T019 [US1] Rework `main.py` into the run lifecycle: compute `digest_date`, run the startup check (exit 0 no-op paths), record the run row, execute fetch → score → digest, commit `digest_sent_at` + run success in one transaction only after the confirmed send, emit `RUN_SUCCESS`/`RUN_FAILED_FINAL` markers, exit non-zero on fatal failure (FR-004, FR-006, FR-017, FR-018) — makes T015 green; depends on T017, T018
- [x] T020 [US1] Run `uv run pytest` — full suite green for the US1 checkpoint
- [x] T021 [P] [US1] Author `infra/main.bicep` core resources: Container Apps environment (Consumption, wired to Log Analytics workspace), Log Analytics workspace, storage account with no public access + Azure Files share, Key Vault (standard) — parameters `location`, `namePrefix` per contracts/deployment.md resource inventory
- [x] T022 [US1] Add the Container Apps Job to `infra/main.bicep`: `Microsoft.App/jobs` Schedule trigger with `cronExpression` param (default `0,20,40 11 * * *` UTC), `parallelism: 1`, `replicaCompletionCount: 1`, `replicaRetryLimit: 0`, `replicaTimeout` ~900 s; system-assigned MI with *Key Vault Secrets User* role; Azure Files volume mount; Key Vault secret refs (`anthropic-api-key`, `smtp-host`, `smtp-port`, `smtp-user`, `smtp-pass`, `digest-to`) surfaced as env vars; plain env `JOBAGENT_DATA_DIR` (mount path), `JOBAGENT_TZ`, `JOBAGENT_MODEL`, `JOBAGENT_MAX_LLM_CALLS`, `JOBAGENT_RETENTION_DAYS`; `imageTag` param — depends on T021
- [x] T023 [US1] Author `infra/main.bicepparam` with non-secret defaults (`location`, `namePrefix`, `cronExpression`, `tz`, `budgetAmount: 50`, `maxLlmCalls`, `retentionDays: 60`); `alertEmail`/`smsCountryCode`/`smsPhone` deliberately absent (never committed; supplied at deploy time) — depends on T022
- [x] T047 [US1] Author `docs/manual-deployment.md`: walkthrough for the initial manual (pre-CI) MVP deployment — bootstrap → manual image build/push to ghcr → `az deployment group create` → Key Vault secrets → runtime-file upload → forced smoke run — honest about MVP state (no CI yet, no alerting until US3), usable by forkers *(maintainer scope addition, 2026-06-11)*
- [ ] T024 [US1] **Maintainer validation** (requires Azure): provision per quickstart.md Bootstrap (manual image push acceptable until US2 lands), set the seven Key Vault secrets (incl. `salary-floor`, added in review), upload the three runtime files, then validate quickstart.md §US1 — digest by 06:00 Pacific with local machines off, empty-day notice, no repeated postings, `RUN_SUCCESS digest_date=<today>` visible in Log Analytics

**Checkpoint**: US1 delivers the MVP — unattended overnight digest in the cloud.

---

## Phase 4: User Story 2 - Push to main ships to production (Priority: P2)

**Goal**: Every push to `main` runs pytest first and deploys (image build → ghcr.io →
Bicep deploy via OIDC) only on green, with no override path and no human action after
the push.

**Independent Test**: Push a trivially observable change and confirm it is live within
15 minutes with tests having run first; push a deliberately failing test and confirm
deployment is blocked while the previous version keeps running (quickstart.md §US2).

### Implementation for User Story 2

*(No pytest tasks: this story is CI/infra wiring with no application-code change; the
workflow's own `test` job is the relevant gate.)*

- [ ] T025 [P] [US2] Create `.github/workflows/deploy.yml` per contracts/deployment.md GitHub Actions contract: trigger push to `main`; permissions `id-token: write`, `packages: write`; job `test` (`uv sync && uv run pytest`) → job `build` (`needs: test`; docker build; push `ghcr.io/<owner>/job-agent:<sha>` public) → job `deploy` (`needs: build`; `az login` via OIDC federated credential; `az deployment group create` with `imageTag=<sha>` plus `alertEmail`/`smsCountryCode`/`smsPhone` from repo secrets `ALERT_EMAIL`/`SMS_COUNTRY_CODE`/`SMS_PHONE`); no `workflow_dispatch` on build/deploy, no `continue-on-error`, no `--debug`, no echoing of parameter values (FR-008, FR-009, FR-011) _(2026-06-13: ships imageTag-only; alert params `alertEmail`/`smsCountryCode`/`smsPhone` deferred to US3/T036 — marker left in the workflow)_
- [ ] T026 [US2] Add the GitHub-OIDC deploy identity (user-assigned managed identity + federated credential, created by `scripts/bootstrap.sh`) to `infra/main.bicep` as referenced/declared resources per the contracts/deployment.md resource inventory and identity & access matrix (FR-022) — depends on T021
- [ ] T027 [US2] **Maintainer validation** (requires Azure + GitHub): run `scripts/bootstrap.sh`, set repo secrets `ALERT_EMAIL`/`SMS_COUNTRY_CODE`/`SMS_PHONE`, then validate quickstart.md §US2 — green path live ≤15 min (SC-003), red path blocks deploy with previous image still serving, Actions logs contain no secret values (FR-011)

**Checkpoint**: US1 and US2 both work — changes now ship themselves.

---

## Phase 5: User Story 3 - Failures are visible by morning coffee (Priority: P3)

**Goal**: Per-source failures degrade gracefully and are named in the digest; scoring
degradation (LLM outage or call-cap exhaustion) is reported, not fatal; a hard failure
fires a platform alert email + SMS independent of the app's SMTP; the on-demand trigger
recovers a failed night; every failure is diagnosable from retained logs.

**Independent Test**: Force a hard failure (broken secret) and a partial failure (bogus
registry slug); confirm both alert channels fire by ~06:30, the degraded digest names
the failed source, the on-demand trigger no-ops/forces correctly, and causes are
identifiable from logs alone (quickstart.md §US3).

### Tests for User Story 3 (write first, observe red)

- [x] T028 [P] [US3] Write failing tests in `tests/test_fetch.py`: a failing adapter does not kill the run; failures are captured as `{source, company_slug, error}` records returned/exposed for the run row instead of stderr-only printing (FR-005)
- [x] T029 [P] [US3] Write failing tests in `tests/test_score.py`: scoring stops after `JOBAGENT_MAX_LLM_CALLS` batches, remaining postings stay `skills_fit IS NULL` for the next run, and the cap-hit/degradation outcome is reported to the caller (FR-013, FR-020) _(⚠️ 2026-06-13 SUPERSEDED: cap landed in Feature 002 via `JOBAGENT_MAX_POSTINGS_PER_RUN`/`JOBAGENT_MAX_COST_PER_RUN`; residual FR-020 report shipped in US3a — e501b48/9704dd9)_
- [x] T030 [P] [US3] Write failing tests in `tests/test_digest.py`: digest body (text + HTML) includes a visible degraded-source notice naming each failed source and a scoring-degradation notice when unscored postings remain (FR-005, FR-020)

### Implementation for User Story 3

- [x] T031 [US3] Rework `src/job_agent/fetch.py` to collect per-source failure records and per-source outcome logging (vendor/slug, error text, counts) while continuing the run — makes T028 green
- [x] T032 [P] [US3] Add the `JOBAGENT_MAX_LLM_CALLS` cap to `src/job_agent/score.py`: stop after N batches, report scored/remaining counts and degradation status — makes T029 green _(⚠️ 2026-06-13 SUPERSEDED by Feature 002's caps; residual FR-020 score-result reporting shipped in US3a — e501b48/9704dd9)_
- [x] T033 [US3] Add degradation notices to `src/job_agent/digest.py` rendering (failed sources by name; scoring backlog note) — makes T030 green; depends on T031, T032
- [x] T034 [US3] Wire degradation through `main.py`: persist `failed_sources` JSON on the run row, set outcome `degraded` when sources failed or the cap was hit, include a human-readable `detail` summary (data-model.md `runs`) — depends on T031–T033
- [x] T035 [US3] Run `uv run pytest` — full suite green for the US3 checkpoint
- [ ] T036 [US3] Add alerting to `infra/main.bicep`: action group with email + SMS receivers (params `alertEmail`, `smsCountryCode`, `smsPhone`, supplied at deploy time only); scheduled query alert rule (30-min evaluation) over `ContainerAppConsoleLogs_CL` with missed-deadline semantics — fires only when local time in `tz` is past the delivery deadline and no `RUN_SUCCESS digest_date=<today's local date>` exists, DST-correct, self-clearing (contracts/deployment.md resource inventory; ⚠️ marker format is a coupled contract with `main.py` — see contracts/runtime-config.md) — depends on T022
- [ ] T037 [US3] **Maintainer validation** (requires Azure): quickstart.md §US3 — **required** alert drill (break a secret or skip a night; both email and SMS arrive by ~06:30, SC-004); on-demand trigger `az containerapp job start` no-ops when today succeeded and runs fully with `--env-vars JOBAGENT_FORCE=1` (FR-019); bogus-slug degraded digest names the source; each cause diagnosable from retained logs alone (SC-005)

**Checkpoint**: Failures alert independently of app email; degradation is visible; recovery is one command.

---

## Phase 6: User Story 4 - Updating the personal files in production (Priority: P4)

**Goal**: The maintainer updates `profile.md`/`screening_prompt.md`/`registry.txt` in
production via `az storage file upload` to the private share — the single sanctioned
manual operation; files are never in the repo, image, or publicly reachable.

**Independent Test**: Upload an edited runtime file, force a run, confirm the new
criteria took effect; confirm anonymous access to the share is denied and the files are
absent from repo and image (quickstart.md §US4).

*(No new code: capability was delivered by `JOBAGENT_DATA_DIR` (Phase 2), the Azure
Files share (T021), and the force-run path (T017/T019); the mechanism is documented in
quickstart.md and contracts/deployment.md.)*

- [ ] T038 [US4] **Maintainer validation** (requires Azure): quickstart.md §US4 — edit `profile.md` locally with an observable criteria change, upload via `az storage file upload`, force a run, confirm the change took effect; verify the storage account refuses anonymous/public access (FR-012) and the personal files appear nowhere in the repo or the published image (inspect image filesystem; FR-021, Constitution VI)

**Checkpoint**: Production criteria evolve without code changes or repo exposure.

---

## Phase 7: User Story 5 - Rebuild everything from the repository (Priority: P5)

**Goal**: The entire production environment is reproducible from repository + secrets +
runtime files in under one hour, with no portal-only steps, following documented
procedure.

**Independent Test**: In a fresh resource group, follow the documented bootstrap
start-to-finish and confirm a scheduled/forced run delivers end-to-end within the hour;
re-apply the deployment and confirm a drift-free no-op (quickstart.md §US5).

### Implementation for User Story 5

- [ ] T039 [P] [US5] Add a "Deploy your own instance" section to `README.md` pointing to `specs/001-azure-deployment/quickstart.md` and listing fork-specific configuration: own subscription/tenant, bootstrap with own repo slug, ghcr package visibility, per-fork repo secrets/variables, own runtime files + Key Vault secrets (plan.md structure)
- [ ] T040 [US5] **Maintainer validation** (requires Azure): quickstart.md §US5 — fresh-resource-group rebuild from repo + secrets + runtime files, timed ≤1 h to a delivered digest (SC-006), no portal-only steps (FR-010); re-run the unchanged `az deployment group create` and confirm it restores/declares the same state (drift check)

**Checkpoint**: All five stories independently validated.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Requirements owned by no single story — retention (FR-015), spend
visibility (FR-014), log-content guarantees (FR-007) — plus final gates.

- [ ] T041 [P] Write failing tests in `tests/test_store.py`: retention purge deletes postings (and their `applications` rows, same transaction) with status `new`/`dismissed`/`duplicate` and `fetched_at` older than `JOBAGENT_RETENTION_DAYS` (default 60); any other status is never purged (FR-015, data-model.md retention rules)
- [ ] T042 Implement the retention purge in `src/job_agent/store.py` and invoke it as a pipeline stage in `main.py` (purge stage logged with counts) — makes T041 green _(⚠️ 2026-06-13: NOT implemented — `JOBAGENT_RETENTION_DAYS` is wired into Bicep but no purge code exists yet; still open)_
- [ ] T043 [P] Add `Microsoft.Consumption/budgets` to `infra/main.bicep`: `budgetAmount` param (default 50), alert thresholds at 50% and 80% notifying the action-group/alert email (FR-014; the Anthropic-side console notification is the documented manual step in quickstart.md Bootstrap §6) — depends on T036
- [ ] T044 Audit per-run log output across `main.py`, `src/job_agent/fetch.py`, `src/job_agent/score.py`, `src/job_agent/digest.py` against the contracts/runtime-config.md minimum-content rules: stage reached, per-source outcomes with error text, fetched/scored/queued counts, fatal failure detail (stage, exception, config key/resource) — and never secret values, runtime-file contents, or scoring rationale (FR-007, SC-005)
- [ ] T045 Final local gate: `uv run pytest` fully green, then `DIGEST_DRY_RUN=1 uv run python main.py` end-to-end against a local db confirming local development is unchanged (quickstart.md "Local development unchanged")
- [ ] T046 **Maintainer validation** (requires Azure): complete quickstart.md top-to-bottom on the production environment and record the ship-acceptance evidence for SC-001 — one unattended scheduled run delivered on time, a passed alert drill, a verified on-demand trigger; cross-check deployed role assignments against the contracts/deployment.md identity & access matrix (FR-022)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies — start immediately
- **Foundational (Phase 2)**: independent of Phase 1 for app tasks (T004–T012); T013 independent — **blocks all user stories**
- **US1 (Phase 3)**: depends on Phase 2; T024 validation additionally needs T003 (image) and T013 (bootstrap)
- **US2 (Phase 4)**: depends on Phase 2 (T013) + T021/T022 (Bicep exists to deploy) + T003 (image builds); T026 depends on T021
- **US3 (Phase 5)**: app tasks depend on Phase 2 + T017/T019 (runs table, lifecycle); T036 depends on T022
- **US4 (Phase 6)**: validation only — depends on US1 deployed (T024) + T013
- **US5 (Phase 7)**: T039 anytime after planning; T040 depends on everything deployable (T013, T021–T023, T025–T026)
- **Polish (Phase 8)**: T041/T042 depend on Phase 2 store work; T043 depends on T036; T044–T046 last

### User Story Dependencies

- **US1 (P1)**: only Foundational — the MVP
- **US2 (P2)**: independent of US1 app code; reuses US1's Bicep file (sequential edits to `infra/main.bicep`)
- **US3 (P3)**: builds on US1's `runs` table and lifecycle; alert rule consumes US1's log markers (coupled contract)
- **US4 (P4)**: no new code; validates US1's storage + Foundational data-dir work
- **US5 (P5)**: documentation + rebuild validation over all prior infra

### Within Each User Story

- Tests written and observed **red** before implementation (Constitution III)
- Store-layer changes before `main.py` orchestration; app code before its `uv run pytest` gate
- `infra/main.bicep` tasks are sequential (same file): T021 → T022 → T026 → T036 → T043
- Maintainer-validation tasks close each story and require a live Azure environment

### Parallel Opportunities

- Phase 1: T002 ∥ T001
- Phase 2: T004, T005, T006, T007, T013 all parallel; then T010 ∥ T011 after T009
- Phase 3: T014, T015, T016, T021 all parallel after Phase 2
- Phase 4: T025 ∥ T026 (different files)
- Phase 5: T028, T029, T030 parallel; T031 ∥ T032 (different files)
- Phase 7/8: T039, T041, T043 parallel with unrelated tasks
- After Phase 2, US1 app work (T014–T020), US1 infra (T021–T023), and US2 CI work (T025) can proceed in parallel streams

---

## Parallel Example: User Story 1

```bash
# After Phase 2, launch all US1 red tests + core Bicep together:
Task: "T014 failing tests for runs table + startup check in tests/test_store.py"
Task: "T015 failing tests for main.py lifecycle/markers in tests/test_main.py"
Task: "T016 failing tests for empty-day notice in tests/test_digest.py"
Task: "T021 author infra/main.bicep core resources"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1 (Dockerfile) + Phase 2 (data-dir, fingerprint migration, model bump, bootstrap)
2. Phase 3: US1 — runs table, lifecycle, empty-day notice, core Bicep, deploy
3. **STOP and VALIDATE** (T024): one unattended overnight digest with machines off
4. The product's entire reason to exist is now live

### Incremental Delivery

1. US1 → unattended digest (MVP) — manual image push acceptable interim
2. US2 → push-to-main CD replaces the manual push
3. US3 → alerting, degradation visibility, on-demand recovery (required alert drill)
4. US4 → validated runtime-file update mechanism
5. US5 → documented, timed rebuild; then Polish (retention, budget, log audit, ship evidence)

### Notes

- The repo's multi-agent TDD workflow applies: test-writer produces the red tests,
  implementer the minimal green diff, reviewer re-runs pytest independently; nothing is
  committed without explicit maintainer approval (AGENTS.md quality gates).
- ⚠️ Coupled contract: the `RUN_SUCCESS`/`RUN_FAILED_FINAL` marker format (T019) and the
  alert query (T036) must change together, re-validated by the alert drill (T037).
- Commit after each task or logical group; never the personal runtime files.
