# Implementation Plan: Cloud-Native Scheduled Operation on Azure

**Branch**: `001-azure-deployment` | **Date**: 2026-06-11 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-azure-deployment/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command. See `.specify/templates/plan-template.md` for the execution workflow.

## Summary

Run the existing fetch → score → digest pipeline unattended in Azure so the triaged
digest email is in the maintainer's inbox by 06:00 America/Los_Angeles every morning,
with test-gated continuous deployment from `main` and infrastructure reproducible
from the repository. Technical approach ([research.md](research.md)): the unchanged
`main.py` runs as an **Azure Container Apps Job** (Consumption plan, Schedule
trigger, cron `0,20,40 11 * * *` UTC) whose three spaced ticks plus an idempotent
`runs`-table check implement the clarified retry policy; state stays in **SQLite on
an Azure Files share** (also home to the private runtime files); secrets live in
**Key Vault** referenced via managed identity; failure alerting is an
absence-of-success **Log Analytics scheduled query alert** firing an action group
(email + SMS); CI/CD is **GitHub Actions with OIDC** (pytest → ghcr.io image →
Bicep deploy); all infrastructure is **Bicep** under `infra/`. Application changes
are the minimal cloud-operation set (research §12): `JOBAGENT_DATA_DIR`, `runs`
table + success markers, empty-day notice email, per-source failure capture, LLM
call cap, retention purge, model default bump to `claude-sonnet-4-6`, Dockerfile.

## Technical Context

**Language/Version**: Python ≥ 3.12, managed with `uv` (hatchling build backend)

**Primary Dependencies**: stdlib-first; `anthropic` (LLM scoring) and `requests`
(ATS adapters) are the only runtime third-party deps. New infra-side tooling:
Docker, Bicep + az CLI, GitHub Actions (no new Python dependencies).

**Storage**: SQLite (`jobs.db`) on an Azure Files SMB share mounted into the job
container; `JOBAGENT_DATA_DIR` prefixes the DB and runtime-file paths (default `.`
keeps local dev unchanged). Single writer guaranteed by FR-017 (research §3).

**Testing**: pytest via `uv run pytest`; stub-based, no network calls, following
existing `tests/` patterns. CI runs the suite as the deploy gate.

**Target Platform**: Azure Container Apps Job (Consumption plan, Linux container),
single production environment, single Azure tenant. Image hosted publicly on
ghcr.io.

**Project Type**: Single Python project (`src/job_agent/`) plus a new `infra/`
directory (Bicep), `Dockerfile`, bootstrap script, and GitHub Actions workflow.

**Performance Goals**: Trivial — one short batch run per day (≈30 new postings,
LLM batches of 6); each tick completes well within a ~900 s replica timeout.
SC-003: push-to-live in under 15 minutes.

**Constraints**: ≤ $50/month all-in across Azure + Anthropic (estimate ≈ $5–15/mo,
research §11); scale-to-zero compute only (FR-016); digest delivered by 06:00
America/Los_Angeles in both DST phases (research §2); at most one concurrent run
(FR-017); no personal data or secrets in repo, image, or CI logs.

**Scale/Scope**: Solo maintainer, tens of companies in the registry, ~3 job
executions/day, database of a few MB. No staging, no multi-user.

No NEEDS CLARIFICATION remain — Phase 0 research resolved every deferred choice
(research.md, "Resolved spec deferrals").

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| # | Principle | Verdict | Evidence |
|---|---|---|---|
| I | Cost Discipline | **PASS** | Full monthly cost table in research §11: ≈$5–15/mo all-in vs $50 ceiling. Consumption-plan job scales to zero (§1); free-tier choices justified against paid alternatives (ghcr vs ACR §5, Bicep vs Terraform §4); dual-bill watching via Azure budget + Anthropic console limit (§10); per-run LLM bound `JOBAGENT_MAX_LLM_CALLS` (§12); cheaper model chosen as default (Sonnet 4.6 over Opus tier, §11). |
| II | Cloud-Native Scheduled Operation | **PASS** | ACA Job schedule trigger, no local dependency (§1–2); all resources declared in Bicep under `infra/` (§4); same `main.py` entry point locally and in production (§1, §3); runtime files updated via the sanctioned private mechanism `az storage file upload` (§3); all config via env/Key Vault (§7). |
| III | Test-First Delivery | **PASS** | GitHub Actions chain pytest → build → deploy with `needs:` as a no-override gate (§9); feature work follows the repo TDD workflow; reviewer re-runs pytest per AGENTS.md. |
| IV | Simplicity & Stdlib-First | **PASS** | No new Python dependencies (§12 uses stdlib `zoneinfo`, existing SMTP path §6); fewest-resources choices: one job not Logic Apps+ACI (§1), SQLite unchanged not a DB rewrite (§3), Bicep not Terraform-with-state-backend (§4), no extra scheduler resource (§2). Complexity Tracking is empty. |
| V | Fail Loud, Fail Visibly | **PASS** | Config/storage/email failures abort hard; absence-of-success alert reaches maintainer via action-group email + SMS, independent of app SMTP (§8); per-source failures degrade gracefully and are reported in the digest (§12.4); Log Analytics retains per-run logs for morning-after diagnosis (§8). |
| VI | Personal-Data Privacy | **PASS** | Runtime files and DB live only on a no-public-access Azure Files share (§3); secrets only in Key Vault, referenced by managed identity, never in repo/CI (§7); public ghcr image contains only public-repo code — personal data injected at runtime, never baked in (§5). |

**Initial check (pre-research)**: PASS — no violations to justify.

**Post-design re-check (after Phase 1 artifacts)**: PASS — the design artifacts
(data-model.md, contracts/, quickstart.md) introduce no new resources, dependencies,
or manual operations beyond those evaluated above; Complexity Tracking remains
empty.

## Project Structure

### Documentation (this feature)

```text
specs/001-azure-deployment/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
│   ├── runtime-config.md    # env vars, exit codes, log-marker contract
│   └── deployment.md        # Bicep params, resource inventory, CI contract
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)

```text
src/job_agent/
├── schema.py            # unchanged
├── store.py             # MODIFIED: runs table, retention purge, data-dir path
├── fetch.py             # MODIFIED: capture per-source failures (FR-005)
├── score.py             # MODIFIED: JOBAGENT_MAX_LLM_CALLS cap, model default bump
├── digest.py            # MODIFIED: empty-day notice email, degraded-source notice
└── adapters/            # unchanged

main.py                  # MODIFIED: runs-table idempotency, RUN_SUCCESS/RUN_FAILED_FINAL markers

tests/                   # NEW tests for every modified module (TDD red-first)

Dockerfile               # NEW: uv-based image running `python main.py`

infra/
├── main.bicep           # NEW: env, job, storage+share, LAW, KV, action group,
│                        #      alert rule, budget, UAMI + federated credential
└── main.bicepparam      # NEW: parameter file (no secret values)

scripts/
└── bootstrap.sh         # NEW: resource group + OIDC deploy identity (chicken-egg)

.github/workflows/
└── deploy.yml           # NEW: test → build/push (ghcr) → az deploy (OIDC)

README.md                # MODIFIED: "Deploy your own instance" section — points to
                         #   specs/001-azure-deployment/quickstart.md and lists the
                         #   fork-specific configuration (own subscription/tenant,
                         #   bootstrap with own repo slug, ghcr package visibility,
                         #   per-fork repo variables, own runtime files + secrets)
```

**Structure Decision**: Single existing Python project; cloud-operation changes
touch five existing modules plus `main.py`, and all new artifacts are additive
(`Dockerfile`, `infra/`, `scripts/bootstrap.sh`, one workflow). No restructuring of
the package layout.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

*(empty — no constitutional violations)*
