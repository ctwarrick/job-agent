# Implementation Plan: LLM Scoring Spend Efficiency

**Branch**: `002-scoring-spend-efficiency` | **Date**: 2026-06-12 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/002-scoring-spend-efficiency/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command. See `.specify/templates/plan-template.md` for the execution workflow.

## Summary

Make LLM scoring cost proportionate to value, implementing Constitution
Principle VII for the **score stage only** (fetch and digest unchanged). Four
capabilities, all inside `src/job_agent/score.py` plus a new pure
`src/job_agent/filter.py` and three small `store.py` additions
([research.md](research.md), [data-model.md](data-model.md)):

1. **Filter before you spend** — a deterministic, zero-cost gate runs before any
   LLM call. A **function denylist** (title keywords for sales / finance /
   clinical / recruiting / marketing / legal …) is the only hard reject; an
   advisory **target-function allowlist**, a **posting-age** gate, and a
   **location** gate refine but never reject alone (allowlist) or fail open on
   missing fields (age/location). Criteria live in a new runtime file
   `filter.toml` (stdlib `tomllib`), git-ignored and delivered via the same
   private mechanism as `profile.md` (Principle II). Rejections persist in a new
   `postings.filter_reason` column so they never re-reach the filter or the LLM.
2. **Per-run budget cap** — `JOBAGENT_MAX_POSTINGS_PER_RUN` (default 200) **and**
   `JOBAGENT_MAX_COST_PER_RUN` (default $5.00); the run stops at whichever binds
   first, loudly (`SCORE_CAP_STOP` log line), preserving the existing
   incremental per-batch commit so the next scheduled run resumes. Supersedes the
   never-implemented `JOBAGENT_MAX_LLM_CALLS` from the 001 contract.
3. **Cache the static prefix** — the screening prompt + profile move into a
   cached `system` block (`cache_control: {"type": "ephemeral"}`); only the
   per-batch postings stay in the volatile user turn, so the static prefix is
   billed once per run instead of once per batch.
4. **Cost observability** — accumulate `usage` across batches and emit one
   `SCORE_SUMMARY` log line (fetched / filtered-with-reason-breakdown / scored /
   remaining; input/output/cache tokens; estimated $) from env-configurable
   per-token prices.

No new third-party dependency (`tomllib` is stdlib ≥ 3.11; `anthropic` already
present). Cost impact: **negative** — the feature exists to cut Anthropic spend
(cold-start ~$21.5 backlog → cents at steady state); no new cloud resources.

## Technical Context

**Language/Version**: Python ≥ 3.12, managed with `uv` (hatchling build backend).

**Primary Dependencies**: stdlib-first. `anthropic` (already present) gains
`cache_control` usage and `usage`-field reads. New: stdlib `tomllib` for the
filter file. No new third-party package, no `pyproject.toml` dependency change.

**Storage**: SQLite (`jobs.db`) via the existing `store.connect()` (the
`nolock=1` Azure Files path is untouched). One additive column
`postings.filter_reason` (idempotent `_migrate` ALTER, like `digest_sent_at`).

**Testing**: pytest via `uv run pytest`; stub-based, no network — the Anthropic
client and `store` queries are monkeypatched exactly as in
[tests/test_score.py](../../tests/test_score.py). New unit tests for the pure
filter need no stubbing at all.

**Target Platform**: unchanged — Azure Container Apps Job (the score stage runs
inside the same `main.py`). This feature is platform-agnostic application logic.

**Project Type**: single Python project (`src/job_agent/`); one new module
(`filter.py`) and one new runtime file (`filter.toml`), no restructuring.

**Performance Goals**: the filter is O(postings) string matching, negligible
versus an LLM call. SC-001: ≥70% of a large board dropped before the LLM.
SC-003: steady-state runs cost cents. The cap bounds the worst case at 200
postings / $5 per run regardless of backlog size.

**Constraints**: ≤ $50/month all-in (Principle I) — this feature *reduces* spend;
filter/cap config must fail loud before any LLM call (Principle V, FR-014); all
new behavior confined to the score stage (FR-013); no personal data in the repo
(Principle VI) — `filter.toml` is git-ignored, only a non-personal
`filter.toml.example` is committed as the schema reference.

**Scale/Scope**: a cold-start board of ~1,285 postings clears over ~7 daily
runs under the 200-cap; steady state is tens of new postings/day. No new scale
concerns.

No NEEDS CLARIFICATION remain — the three spec markers were resolved in the
2026-06-12 `/speckit-clarify` session (spec `## Clarifications`); Phase 0 below
resolves the remaining design choices.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| # | Principle | Verdict | Evidence |
|---|---|---|---|
| I | Cost Discipline | **PASS** | The feature's purpose is to bound and reduce LLM spend: pre-filter (SC-001 ≥70% cut), prompt caching (static prefix billed once/run), and a hard per-run cap (FR-005/006/008). No new cloud resource; expected monthly impact is a spend *reduction*. Model stays selectable (`JOBAGENT_MODEL`); pricing inputs are env-configurable (FR-012). |
| II | Cloud-Native Scheduled Operation | **PASS** | `main.py` entry point unchanged; the score stage still runs in the same scheduled job. `filter.toml` is updated through the existing sanctioned private mechanism (Azure Files upload), same as `profile.md`/`screening_prompt.md` (research §3). All new config via env vars + the runtime file. |
| III | Test-First Delivery | **PASS** | TDD: failing tests for the pure filter, the cap loop, the resume query, and the cost summary are authored red before implementation; `uv run pytest` is the green gate; the reviewer re-runs it (AGENTS.md). |
| IV | Simplicity & Stdlib-First | **PASS** | No new third-party dep (`tomllib` is stdlib). One new module, `filter.py`, is pure functions (`load_criteria`, `classify`) — the preferred small-pure-function shape over a class. Filter criteria are runtime-file tuning, not code (FR-002). Complexity Tracking is empty. |
| V | Fail Loud, Fail Visibly | **PASS** | Missing/malformed `filter.toml` or invalid cap env vars `sys.exit` before any LLM call (FR-014, research §4), matching score.py's existing `sys.exit` on missing key/floor. Cap stop and run summary are emitted as greppable stdout markers (`SCORE_CAP_STOP`, `SCORE_SUMMARY`) for morning-after diagnosis. |
| VI | Personal-Data Privacy | **PASS** | `filter.toml` (which encodes personal target functions/metros) is git-ignored and lives only in the runtime environment; only a generic, non-personal `filter.toml.example` is committed. No posting content or profile text enters logs — the summary reports counts and token totals only. |
| VII | LLM Spend Efficiency | **PASS** | This feature *is* the implementation of Principle VII for scoring: filter-before-spend (US1), cache the static prefix (US3), configurable per-run cap (US2), per-run cost observability (US4). Each principle bullet maps to an FR (FR-001/009/005/011). |

**Initial check (pre-research)**: PASS — no violations to justify.

**Post-design re-check (after Phase 1 artifacts)**: PASS — the design artifacts
([data-model.md](data-model.md), [contracts/](contracts/),
[quickstart.md](quickstart.md)) add one additive column, one stdlib import, one
pure module, and four env vars; no new resource, dependency, or manual
operation beyond the already-sanctioned runtime-file upload. Complexity Tracking
remains empty.

## Project Structure

### Documentation (this feature)

```text
specs/002-scoring-spend-efficiency/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
│   ├── filter-criteria.md   # filter.toml schema + gate semantics
│   └── runtime-config.md     # new env vars, log markers, exit behavior
├── checklists/
│   └── requirements.md  # spec quality checklist (16/16)
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
src/job_agent/
├── filter.py            # NEW: pure deterministic gate — load_criteria(path) -> Criteria,
│                        #      classify(posting, criteria) -> reason | None (no LLM, no I/O beyond load)
├── score.py             # MODIFIED: call filter before LLM; cached system prefix
│                        #      (cache_control); per-run cap loop; usage accumulation + SCORE_SUMMARY
├── store.py             # MODIFIED: filter_reason column (_migrate); scorable() query;
│                        #      record_filter_rejections() helper
├── schema.py            # unchanged
├── fetch.py             # unchanged (FR-013)
├── digest.py            # unchanged (FR-013 — rejected rows already excluded: skills_fit IS NULL)
└── adapters/            # unchanged

main.py                  # unchanged — score.main() still returns normally on a cap stop,
                         #   so the run outcome stays success/degraded (Assumptions)

filter.toml              # NEW runtime file (git-ignored; personal target functions/metros)
filter.toml.example      # NEW committed schema reference (generic, non-personal)

.gitignore               # MODIFIED: add filter.toml (keep filter.toml.example tracked)

tests/
├── test_filter.py       # NEW: pure-function gate tests (denylist/allowlist/age/location, fail-open)
├── test_score.py        # MODIFIED: cap stop, cached-prefix shape, cost summary, scorable() use
└── test_store.py        # MODIFIED: filter_reason migration + scorable()/record_filter_rejections

README.md                # MODIFIED: note filter.toml in the runtime-files / "deploy your own" list
```

**Structure Decision**: single existing Python project. The deterministic gate
is isolated in a new pure `filter.py` (testable with zero stubbing); everything
else is additive edits to `score.py`/`store.py` plus one runtime file. No
package-layout change; `main.py`, `fetch.py`, `digest.py`, `schema.py`, and the
adapters are untouched.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

*(empty — no constitutional violations)*
