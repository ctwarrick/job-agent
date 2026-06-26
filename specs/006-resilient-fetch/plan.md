# Implementation Plan: Resilient, Time-Bounded ATS Fetching

**Branch**: `006-resilient-fetch` | **Date**: 2026-06-26 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/006-resilient-fetch/spec.md`

## Summary

The daily pipeline stopped delivering once a large-board adapter (Workday) went
live: each in-scope adapter fetches the **full per-posting description for every
listed job before any filtering**, so cost scales with board size and a ~900-req
board exhausts the ~15-minute execution window. A single per-item error also
discards the whole board, and the per-source "new" count is double-counted.

The fix has four moving parts, applied as one **shared orchestration contract**
across the three in-scope adapters (Workday, iCIMS, Talemetry):

1. **Filter before you spend** — list cheaply (listing-level fields only), run
   the existing deterministic filter (`filter.classify`, which already decides
   on title/location/posted_at), and fetch the expensive description **only for
   survivors** (FR-003/FR-004). Removes the `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER`
   per-employer cap, which FR-002 rejects.
2. **Per-item resilience** — one failed detail/page is logged and skipped; the
   source keeps the postings it already collected (FR-005/FR-006).
3. **Bounded with a fail-loud backstop + forward progress** — a configurable
   per-source detail cap and wall-clock deadline (FR-013) stop a pathological
   board before it blows the window; the store itself is the progress state, so
   each run describes only **not-yet-stored** survivors and the backlog drains;
   a source that stays truncated past a configurable staleness bound is surfaced
   as a persistent degradation (FR-015).
4. **Accurate count** — `upsert_postings` returns only the postings-insert count,
   not the companion applications insert (FR-011).

Partial-source degradation (skips, backstop truncation, stuck-beyond-bound) is
surfaced **in the digest**, distinct from healthy and wholly-failed sources
(FR-014). Out-of-scope single-request adapters (Greenhouse, Lever) are untouched
(FR-008). No schema wipe; the unscored backlog drains automatically (FR-009).

## Technical Context

**Language/Version**: Python 3.12 (`requires-python = ">=3.12"`).

**Primary Dependencies**: `requests` (existing, HTTP), `beautifulsoup4` (existing,
Talemetry HTML), `anthropic` (existing, scoring — untouched). **No new dependency.**

**Storage**: SQLite `jobs.db` via `store`. One **additive** migration (a tiny
`source_progress` table + idempotent `ALTER`-style create); no destructive change,
no re-key, existing rows/scores/application-status preserved (FR-009).

**Testing**: `pytest`, stub-based, no network — adapters monkeypatch
`adapter.requests` / `adapter.time.sleep` (existing pattern in `tests/`); store
and orchestration tested against `:memory:` / `tmp_path` DBs and fake clocks.

**Target Platform**: Linux — local dev and Azure consumption compute.

**Project Type**: single CLI/pipeline project (`src/job_agent/`).

**Performance Goals**: the fetch stage MUST leave margin inside the ~15-min window
for scoring + digest. Expensive description retrievals scale with **filter
survivors**, not board size (SC-004); a pathological board is bounded by the
backstop and converges across runs (SC-008/SC-010).

**Constraints**: no per-employer cap (FR-002); descriptions never fetched for
filter-rejected postings (FR-003); per-item skip-not-abort (FR-005); no schema
wipe (FR-009); stub-based tests, TDD red-first (FR-012); ≤100-char lines all files.

**Scale/Scope**: three in-scope adapters refactored to a two-phase shape; one new
shared orchestration path; ~3 new `store` functions + one tiny table; the digest
degradation renderer extended with a "partial" category; one `upsert` count fix.

## Constitution Check

*GATE: passed before Phase 0; re-checked after Phase 1 design (below).*

- **I. Cost Discipline** — PASS. No new cloud resources; **monthly cost impact
  $0**. The change *reduces* spend: filter-before-detail cuts both HTTP detail
  fetches and the number of postings reaching the LLM. The backstop is a hard
  upper bound on per-run fetch work.
- **II. Cloud-Native Scheduled Operation** — PASS. No new infra; runs in the
  existing `main.py` fetch stage, locally runnable with env + git-ignored runtime
  files. New behavior is config-via-env (backstop budget, staleness bound).
- **III. Test-First Delivery** — PASS (process gate). Red-first stub tests; `uv run
  pytest` green before done; reviewer re-runs. No infra touched → `validate-infra.sh`
  N/A.
- **IV. Simplicity & Stdlib-First** — PASS. No new dependency. New state is **one
  tiny table** (`source_progress`, one timestamp per source) — justified in
  Complexity Tracking as the minimal honest mechanism for FR-015's cross-run
  staleness alert. Shared orchestration is small pure helpers, not a framework.
- **V. Fail Loud, Fail Visibly** — PASS. Per-item failures logged+skipped; backstop
  truncation and stuck-beyond-bound sources surfaced as visible digest degradation
  (FR-013/FR-014/FR-015); config errors still fail hard before any external effect.
- **VI. Personal-Data Privacy** — PASS. No personal data in artifacts; boards are
  named generically (~900-req board). Registry/profile/db stay git-ignored.
- **VII. LLM Spend Efficiency** — PASS, and **strengthened**. This feature is the
  fetch-stage embodiment of "filter before you spend": the deterministic gate now
  runs *before* the expensive description retrieval, not just before the LLM. The
  backstop is the fetch-stage analogue of Principle VII's "budget guardrail / stop
  loudly."

**Initial gate: PASS.** No violations requiring Complexity Tracking beyond the
`source_progress` table (recorded below).

## Key design decisions

### D1 — Two-phase adapter contract (in-scope adapters)

Each in-scope adapter splits into two primitives so the bounded/resilient/forward-
progress logic lives **once** in shared orchestration (FR-007), not duplicated:

- `list_postings(slug, *, company, timeout) -> list[Posting]` — paginate the
  **listing only**, returning listing-level `Posting` stubs (title, location,
  posted_at, url, external_id; `description=""`). Cheap; full-board.
- `fetch_description(posting, *, timeout) -> str` — the one expensive per-posting
  call. **iCIMS:** descriptions are inline, so its stubs already carry
  `description` and its `fetch_description` is a pass-through (no second round-trip).

Out-of-scope Greenhouse/Lever keep their single `fetch(slug, *, company=...)`.

### D2 — Shared orchestration (the contract)

A shared helper (new `src/job_agent/resilient.py`, or a private function in
`fetch.py`) drives every in-scope source:

```
stubs      = adapter.list_postings(slug, company=company)   # per-page resilient
survivors  = [s for s in stubs if classify(s, criteria) is None]   # FR-003/4
already    = store.existing_external_ids(source, company)   # forward-progress state
todo       = [s for s in survivors if s.external_id not in already]
described, skipped, truncated = [], 0, False
deadline   = clock() + DEADLINE_SECONDS
for i, s in enumerate(todo):
    if i >= MAX_DETAIL_PER_SOURCE or clock() > deadline:
        truncated = True; break                              # FR-013 backstop
    try:
        desc = s.description or adapter.fetch_description(s)  # iCIMS short-circuit
        described.append(replace(s, description=desc))
    except Exception:
        skipped += 1; log                                    # FR-005 skip-not-abort
store.upsert_postings(described)                             # stable fingerprint
return SourceResult(new=..., skipped=skipped, truncated=truncated,
                    remaining=len(todo)-i_processed, persistent=...)
```

- **Forward progress (FR-015)** falls out of `already`: a survivor described and
  stored is skipped next run, so runs never re-truncate the same prefix; the
  backlog converges in ⌈survivors/budget⌉ runs.
- **Listing-level filter input**: `classify` indexes `posting["title"]` etc.;
  stubs are passed as a small dict view (`{"title":…, "location":…, "posted_at":…}`).

### D3 — Identity is preserved; no re-key (FR-009)

`Posting.fingerprint` includes `description`, so a stub stored with `description=""`
would change identity once described. We therefore **never store description-less
stubs**: a survivor is stored only *after* its description is fetched, so the
fingerprint is final at insert. Forward-progress is tracked by the
description-independent **`external_id`** (already a column), via
`store.existing_external_ids`. `schema.py` and the fingerprint scheme are
**unchanged**. Rejected postings from in-scope adapters are simply not stored;
they are re-derived cheaply (listing-level, no LLM, no detail call) each run.

### D4 — Bounded-staleness alert needs minimal cross-run state (FR-015)

Forward progress guarantees convergence *when budget ≥ survivor inflow*. To turn
"structurally behind forever" into a loud failure, we track **one timestamp per
source**: `source_progress(source, company, last_converged_at)`. A run that
finishes a source with `remaining == 0` sets `last_converged_at = now`. A run that
leaves `remaining > 0` and finds `now - last_converged_at > STALENESS_BOUND` marks
that source **persistently degraded** → surfaced in the digest (FR-015). New
sources seed `last_converged_at = now` (a full grace window).

### D5 — Backstop config (replaces the rejected per-employer cap)

| Env var | Default | Meaning |
|---|---|---|
| `JOBAGENT_MAX_DETAIL_PER_SOURCE` | 150 | max expensive detail fetches / source / run |
| `JOBAGENT_FETCH_DEADLINE_SECONDS` | 300 | wall-clock per-source fetch budget |
| `JOBAGENT_STALENESS_BOUND_DAYS` | 7 | days truncated before persistent-degradation alert |

`JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` is **removed** from the adapters. The registry
`max_per_employer` key stays *accepted-but-ignored* (documented deprecated) so an
existing git-ignored `registry.toml` does not fail-loud on load.

### D6 — Partial-degradation reporting (FR-014)

`fetch.main` already returns failed-source records; extend it to also return
**partial** records (`{source, company_slug, new, skipped, truncated, persistent}`).
`digest._degradation_*` gains a "partial / degraded" category, rendered distinct
from "unreachable" (failed) and healthy sources.

### D7 — FR-011 count fix

`store.upsert_postings` currently returns `conn.total_changes - before`, which
counts the postings insert **and** the companion applications insert (2× for
all-new). Fix: capture the postings-insert delta alone (snapshot `total_changes`
between the two `executemany` calls) and return that.

## Project Structure

### Documentation (this feature)

```text
specs/006-resilient-fetch/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   └── resilient-fetch.md   # Phase 1 — orchestration + adapter + store contract
├── checklists/
│   └── requirements.md  # pre-existing spec-quality checklist (16/16)
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
src/job_agent/
├── resilient.py         # NEW — shared list→filter→forward-progress→describe→upsert
├── fetch.py             # EDIT — route in-scope adapters through resilient.py;
│                        #        return partial records alongside failures
├── store.py             # EDIT — upsert count fix (FR-011); existing_external_ids();
│                        #        source_progress table + get/set; additive migration
├── digest.py            # EDIT — render "partial / degraded" category (FR-014)
├── adapters/workday.py  # EDIT — split into list_postings + fetch_description;
│                        #        drop per-employer cap
├── adapters/icims.py    # EDIT — list_postings (inline desc); fetch_description pass-
│                        #        through; per-page resilience; drop per-employer cap
├── adapters/talemetry.py# EDIT — split list_postings + fetch_description; drop cap
├── adapters/greenhouse.py  # unchanged (FR-008)
├── adapters/lever.py       # unchanged (FR-008)
├── registry.py          # EDIT (optional) — deprecate max_per_employer note
└── schema.py            # unchanged (fingerprint scheme preserved — D3)

tests/
├── test_resilient.py    # NEW — orchestration: filter-before-detail, skip-not-abort,
│                        #        backstop, forward-progress, staleness alert
├── test_workday.py      # EDIT — two-phase + bounded detail + no per-employer cap
├── test_icims.py        # EDIT — two-phase + per-page resilience
├── test_talemetry.py    # EDIT — two-phase
├── test_store.py        # EDIT — upsert count = inserts only; new store fns
├── test_fetch.py        # EDIT — partial records returned
└── test_digest.py       # EDIT — partial-degradation rendering
```

**Structure Decision**: single-project pipeline. The new `resilient.py` centralizes
the shared contract (FR-007) so the three adapters stay thin and identical in
behavior; the dark Talemetry adapter gets the fix even though no live source is
wired (FR-007), proven by its stub suite (SC-006).

## Phase 0 — Research

See [research.md](research.md). Resolves: where the filter/backstop/forward-progress
logic lives (shared `resilient.py` vs. per-adapter); the listing-vs-description
split per adapter (incl. iCIMS inline-description short-circuit); the
fingerprint-identity constraint and why stubs are never stored description-less
(D3); the minimal cross-run staleness state (D4); and the backstop config surface
(D5). No NEEDS CLARIFICATION remain.

## Phase 1 — Design & Contracts

- [data-model.md](data-model.md) — the unchanged `Posting`, the new
  `source_progress` table, the listing-stub vs. described-posting states, and the
  forward-progress/convergence lifecycle.
- [contracts/resilient-fetch.md](contracts/resilient-fetch.md) — the two-phase
  adapter contract, the `resilient.run_source` orchestration contract, the new
  `store` functions, the backstop env surface, and the `SourceResult` →
  digest-degradation mapping.
- [quickstart.md](quickstart.md) — runnable validation scenarios per user story.
- Agent context: the SPECKIT block in `AGENTS.md` (CLAUDE.md symlink) repointed to
  this plan.

### Post-Design Constitution Re-Check

No change after design. Still no new dependency; the only new state is the one-
timestamp `source_progress` table (additive, non-destructive — FR-009 holds); no
infra; new behavior is config-via-env and fail-loud. Gate still **PASS**.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| New `source_progress` table (Constitution IV — added state) | FR-015 requires a loud alert when a source stays truncated **across runs**; that escalation needs cross-run memory the stateless store does not hold | A pure offset/cursor table is heavier; deriving staleness from row timestamps cannot distinguish "newly added big board, still draining (fine)" from "structurally stuck (alert)"; one `last_converged_at` per source is the minimal state that does |
