# Implementation Plan: Overnight Run Scaling

**Branch**: `main` (feature dir `007-overnight-scale`) | **Date**: 2026-07-09 | **Spec**:
[spec.md](spec.md)

**Input**: Feature specification from `specs/007-overnight-scale/spec.md`

## Summary

The overnight pipeline stopped delivering after the registry grew to ~35 Workday-heavy
boards: a full sequential fetch exceeds the job's execution window and the platform kills
the run before scoring/digest. This feature makes run capacity scale with the registry
along three axes, all already clarified in the spec:

1. **Bigger window, tiled schedule (US1)** — raise the Container Apps Job `replicaTimeout`
   to a 2-hour window and move the three daily attempts to a non-overlapping,
   after-local-midnight cadence via `infra/main.bicep(param)`. (A stopgap subset of this
   already shipped in v2.4.2; this feature makes it the designed end state.)
2. **Fetch-stage time budget (US2)** — a stage-level wall-clock cap in `fetch.main()`,
   peer to the existing score-stage `MAX_POSTINGS`/`MAX_COST` caps: on expiry fetch stops
   cleanly, keeps everything fetched, and the run proceeds to score/digest. Boards not
   reached are ordered first next run (least-recently-fully-fetched), and a startup check
   fails loud if the budget + reserved headroom cannot fit the window.
3. **Board-level concurrent fetch (US3)** — fetch multiple boards in parallel with a
   bounded worker pool so the enlarged registry finishes well inside the budget. 006's
   per-source backstops (detail cap, per-source deadline, forward progress) are unchanged;
   only the dispatch loop in `fetch.main()` becomes concurrent.

The work is a targeted change to `fetch.py` plus its supporting `store.py`/config surface
and the Bicep infra; no new Azure resources and no new Python dependencies.

## Technical Context

**Language/Version**: Python ≥ 3.12 (`uv`, `hatchling`)

**Primary Dependencies**: stdlib-first; `anthropic` (scoring) and `requests` (adapters)
are the only heavyweight deps. Concurrency uses stdlib `concurrent.futures.ThreadPoolExecutor` —
no new dependency.

**Storage**: SQLite (`jobs.db`) on an Azure Files share; `source_progress` table already
holds `last_converged_at` per (source, company) for 006 forward progress and is the basis
for the least-recently-fetched ordering.

**Testing**: `pytest`, stub-based, no network — extend the patterns in `tests/test_fetch.py`
and `tests/test_resilient.py`. Infra changes re-validated by `scripts/validate-infra.sh`.

**Target Platform**: Azure Container Apps Job (Consumption), scheduled trigger, one
replica, `replicaRetryLimit: 0` (retries are separate scheduled attempts, not platform
replica retries).

**Project Type**: Single-project CLI pipeline (`src/job_agent/`), one module per stage.

**Performance Goals**: Fetch-stage wall-clock for ~50 Workday-heavy boards drops ≥3x vs.
sequential (SC-004); full run completes inside the 90-min fetch budget on a healthy night
with ≥30-min headroom for score/digest (SC-001/SC-002).

**Constraints**: 2-hour execution window; three attempts at 00:00/02:00/04:00 PST
(01:00/03:00/05:00 PDT) via fixed UTC cron `0 8,10,12`, all after local midnight in both DST
states (preserve run-start `digest_date`). A fixed-UTC schedule cannot also keep the final
attempt strictly ≤ 06:00 local in both zones (research R2 proves the constraints are
over-determined); we prioritize the after-midnight/`digest_date` invariant and accept the
rare PDT-edge slip (third attempt can reach 07:00 PDT only if attempts 1–2 both failed *and*
the third runs > 1h — the missed-deadline alert tolerates a one-eval slip). Attempt spacing
equals the window (2h); the exact boundary is backstopped by the existing in-flight startup
check (spec assumption), with spacing an efficiency measure on top. Concurrent writes to one
SQLite file must be serialized.

**Scale/Scope**: Design target ~50 boards (today 35: 6 Greenhouse, 3 Lever, 26 Workday).
The bottleneck is Workday per-posting detail fetching, not raw board count.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle | Assessment |
|---|---|
| **I. Cost Discipline** | PASS. No new Azure resources. The longer *permitted* window costs nothing when the run finishes early (consumption billing follows actual duration); concurrency shortens actual duration, so expected monthly compute impact is ~$0 net and stays within the $50 all-in ceiling (SC-005). Plan states cost impact explicitly here per the rule. |
| **II. Cloud-Native Scheduled Op** | PASS. Schedule/window are IaC (`infra/main.bicep(param)`); no portal edits. New behavior is config-via-env (fetch budget, headroom, concurrency, execution-window). Pipeline stays runnable locally with `concurrency=1` reproducing today's sequential path. |
| **III. Test-First** | PASS (enforced in build). TDD ordering per Phase 1 test list; `uv run pytest` + `scripts/validate-infra.sh` are the green gates. |
| **IV. Simplicity & Stdlib-First** | PASS. `ThreadPoolExecutor` is stdlib; no new dependency. Threads (not asyncio) chosen because adapters are synchronous `requests` code — see research R1. Fetch budget reuses the existing `_positive_int_env` cap pattern. |
| **V. Fail Loud** | PASS. FR-004 startup validation exits non-zero before any external effect on incoherent budget/window config; invalid knob values fail loud (FR-010). Budget-truncated fetch is surfaced visibly in the digest (FR-005), consistent with 006's degraded reporting. |
| **VI. Personal-Data Privacy** | PASS. No `profile.md`/`registry.toml`/`jobs.db` contents in any artifact; only board *counts* and vendor names appear. |
| **VII. LLM Spend Efficiency** | PASS / N/A. The fetch stage makes no LLM calls; the score-stage caps and caching are untouched. The new fetch budget is a *time* peer of the existing spend caps, not a change to scoring. |

**Result**: No violations. Complexity Tracking table left empty.

## Project Structure

### Documentation (this feature)

```text
specs/007-overnight-scale/
├── plan.md              # This file
├── research.md          # Phase 0: threading model, DST cron, window-config, ordering
├── data-model.md        # Phase 1: source_progress ordering, config knobs, budget state
├── quickstart.md        # Phase 1: validation scenarios (stub-based)
├── contracts/
│   ├── runtime-config.md # env knobs + startup-validation contract
│   └── fetch-stage.md    # concurrency + budget behavior contract
└── tasks.md             # Phase 2 (/speckit-tasks — not created here)
```

### Source Code (repository root)

```text
src/job_agent/
├── fetch.py       # CHANGED: concurrent board dispatch, stage budget, LRF ordering,
│                  #          budget-deferred reporting into (failed, partial) tuple
├── resilient.py   # UNCHANGED semantics: per-source backstops stay as-is; may expose
│                  #          a helper so a board's own deadline is min(source, stage-remaining)
├── store.py       # CHANGED: convergence read for ALL vendors + ordering query;
│                  #          greenhouse/lever get a converged timestamp on full fetch
├── score.py       # UNCHANGED
├── digest.py      # CHANGED: render the new "fetch budget truncated / N boards deferred"
│                  #          degraded category (extends 006's partial-source reporting)
└── main.py        # CHANGED: FR-004 startup validation (budget+headroom ≤ window)

infra/
├── main.bicep       # CHANGED: replicaTimeoutSeconds default → 7200; cron default;
│                    #          any new pass-through env (execution-window) for validation
└── main.bicepparam  # CHANGED: pin 2h window + tiled cron as the designed end state

tests/
├── test_fetch.py      # concurrency equivalence, stage-budget stop, LRF ordering
├── test_resilient.py  # unchanged per-source behavior under a stage-remaining deadline
├── test_store.py      # ordering query, cross-vendor convergence
├── test_main.py       # startup validation fail-loud
└── test_digest.py     # budget-deferred degraded category
```

**Structure Decision**: Single-project layout unchanged. The feature is concentrated in
`fetch.py` (dispatch loop) with supporting changes in `store.py` (ordering/convergence),
`main.py` (startup validation), `digest.py` (reporting), and the Bicep infra. `resilient.py`
per-source logic is preserved; board-level concurrency wraps it rather than rewriting it.

## Complexity Tracking

No constitution violations — no entries.
