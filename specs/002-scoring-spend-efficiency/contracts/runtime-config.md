# Contract: Runtime Configuration (score stage)

**Feature**: `002-scoring-spend-efficiency` | **Date**: 2026-06-12

New environment variables and stdout log markers introduced by the spend-
efficiency feature. Scope is the **score stage only** (FR-013); fetch and digest
config are unchanged. Extends the 001 runtime-config contract
([../../001-azure-deployment/contracts/runtime-config.md](../../001-azure-deployment/contracts/runtime-config.md)).

## New environment variables

| Variable | New? | Required | Default | Purpose |
|---|---|---|---|---|
| `JOBAGENT_MAX_POSTINGS_PER_RUN` | **new** | no | `200` | hard cap on postings scored per run (FR-005); the run stops at this OR the cost cap, whichever binds first |
| `JOBAGENT_MAX_COST_PER_RUN` | **new** | no | `5.00` | hard cap on **estimated** dollars per run (FR-005); pre-call projection, so actual spend ≤ cap (research §5) |
| `JOBAGENT_PRICE_INPUT` | **new** | no | `3.00` | $/MTok, uncached input — cost estimation (FR-012) |
| `JOBAGENT_PRICE_OUTPUT` | **new** | no | `15.00` | $/MTok, output tokens |
| `JOBAGENT_PRICE_CACHE_WRITE` | **new** | no | `3.75` | $/MTok, cache-creation tokens (1.25× input) |
| `JOBAGENT_PRICE_CACHE_READ` | **new** | no | `0.30` | $/MTok, cache-read tokens (0.10× input) |

Defaults track documented `claude-sonnet-4-6` list pricing so an unconfigured
deployment still bounds and estimates spend sensibly. All are plain (non-secret)
env entries — they carry no personal data.

**Validation (fail-loud, FR-014)**: when set, each cap must parse as a number > 0
and each price as a number ≥ 0; otherwise the score stage `sys.exit`s **before**
any LLM call (research §4), alongside the existing missing-key / missing-floor
exits. An unset variable falls back to its default (the system is never
unbounded — FR-008).

### Supersedes

This contract **supersedes** the never-implemented `JOBAGENT_MAX_LLM_CALLS` from
the 001 runtime-config contract (a per-run batch-count cap). The two
`*_PER_RUN` caps here express the bound directly in the units that matter
(postings and dollars) and are honored at posting granularity. The 001 contract's
`JOBAGENT_MAX_LLM_CALLS` row should be treated as replaced; no code ever read it.

## Log markers (informational — NOT consumed by the 001 alert rule)

Emitted to stdout, captured in `ContainerAppConsoleLogs_CL`. Unlike the 001
`RUN_SUCCESS` / `RUN_FAILED_FINAL` markers, these are **diagnostic only** — the
missed-deadline alert rule does **not** consume them, so their format is not an
alert-breaking contract (it is still kept stable for grep-ability and the
quickstart assertions).

| Marker | When | Content |
|---|---|---|
| `SCORE_SUMMARY` | once per run (every run that reaches the score stage, including zero-LLM runs) | `fetched=<n> filtered=<n> filtered_by_reason=function_denylist:<n>,age:<n>,location:<n> scored=<n> remaining=<n> input_tokens=<n> output_tokens=<n> cache_write_tokens=<n> cache_read_tokens=<n> est_cost_usd=<f.ff>` |
| `SCORE_CAP_STOP` | only when a cap bound the run | `reason=postings\|cost scored=<n> remaining=<n> limit=<value>` — emitted immediately before the run's `SCORE_SUMMARY` |

No posting content, profile text, or scoring rationale appears in either marker —
counts and token totals only (Constitution Principle VI; FR-011).

## Exit behavior

| Situation | Exit | Rationale |
|---|---|---|
| Normal completion (all scorable postings scored) | `0` | success |
| **Cap stop** (postings or cost cap reached, partial progress committed) | `0` | a cap stop is a **normal** event for a large backlog draining over several runs; it must NOT mark the scheduled job failed or trip the 001 missed-deadline alert (spec Assumptions; research §5). `SCORE_CAP_STOP` + `SCORE_SUMMARY` make the partial progress and remaining backlog explicit |
| Empty post-filter set (every posting filtered out) | `0` | zero LLM calls, `SCORE_SUMMARY` with `scored=0`; not an error (spec edge case) |
| Missing/malformed `filter.toml`; invalid cap/price env var | non-zero (`sys.exit`) | fail-loud before any LLM call (FR-014) |
| Missing `ANTHROPIC_API_KEY` / `JOBAGENT_SALARY_FLOOR` | non-zero (`sys.exit`) | existing behavior, unchanged |

Because a cap stop returns normally, `main.py`'s run outcome stays
success/degraded (the run is not marked failed for a routine cap event), and the
next scheduled run resumes the remaining scorable backlog via `store.scorable()`
(FR-007).
