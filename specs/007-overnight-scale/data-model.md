# Phase 1 Data Model: Overnight Run Scaling

No new tables. This feature extends existing state and adds config, not schema breadth.

## Entities

### Board fetch state (extends `source_progress`)

The 006 `source_progress` table `(source, company, last_converged_at)` becomes the ordering
key for the whole registry.

- **Change**: single-request vendors (`greenhouse`, `lever`) now also write
  `last_converged_at` via `mark_converged` on a successful whole-board fetch. Previously
  only resilient vendors (`workday`/`icims`/`talemetry`) did.
- **Ordering rule**: dispatch order = `ORDER BY last_converged_at ASC NULLS FIRST` over the
  registry's sources. Never-fetched boards (no row) sort first; the longest-stale board
  sorts next. A board that is dispatched but truncated by the *stage* budget keeps its
  prior (older or NULL) timestamp, so it stays ahead next run.
- **Validation / invariant**: `last_converged_at` advances only on a *full* fetch of that
  board (resilient: `remaining == 0`; single-request: the call returned without raising).
  A stage-budget deferral must not advance it (else the board could starve — SC-003).
- **Starvation signal**: unchanged from 006 — a board unconverged longer than
  `JOBAGENT_STALENESS_BOUND_DAYS` is `persistent=True` and surfaces as a standing problem.

### Fetch-stage budget (runtime value, not persisted)

- A per-run monotonic deadline `stage_deadline = clock() + JOBAGENT_FETCH_BUDGET_SECONDS`,
  computed once at the top of `fetch.main()`.
- Governs **submission**: no new board is submitted after `clock() > stage_deadline`.
- Each dispatched board receives an effective deadline `min(per_source_deadline,
  stage_deadline)` so an in-flight board cannot overrun the stage stop (research R5).
- Distinct from 006's per-source `JOBAGENT_FETCH_DEADLINE_SECONDS`, which bounds one board.

### Fetch-stage outcome (extends the `(failed, partial)` return contract)

`fetch.main()` today returns `(failed_sources, partial_sources)`. This feature adds a
third state without breaking the shape:

- **Deferred boards**: sources never dispatched because the stage budget expired first.
  Represented as `partial_sources` entries (or a parallel `deferred` list — build decides)
  carrying `{source, company_slug, reason="budget_deferred"}`. Rendered by `digest.py` as a
  distinct degraded category (FR-005), separate from failed and per-source-partial.
- The run outcome stays `degraded` (not `failed`) when boards are deferred but a digest is
  sent — consistent with `main.py`'s existing `degraded` logic (deferred is non-fatal).

## Configuration knobs (all env; fail loud on invalid — FR-010)

| Env var | Type | Default | Meaning |
|---|---|---|---|
| `JOBAGENT_FETCH_CONCURRENCY` | positive int | 8 | Max boards fetched in parallel; 1 = sequential |
| `JOBAGENT_FETCH_BUDGET_SECONDS` | positive int | 5400 | Stage-level wall-clock budget for fetch |
| `JOBAGENT_SCORE_DIGEST_HEADROOM_SECONDS` | positive int | 1800 | Reserved tail for score + digest |
| `JOBAGENT_EXECUTION_WINDOW_SECONDS` | positive int | none — required; Bicep sets = replicaTimeout | Window the app validates against |

**Startup validation (FR-004, in `main.py` before any effect)**:
`FETCH_BUDGET + SCORE_DIGEST_HEADROOM ≤ EXECUTION_WINDOW`, else `sys.exit()` naming all
three values. Also each knob individually fails loud on non-positive via `_positive_int_env`.

## Concurrency-safety invariants

- **Single SQLite writer**: all `upsert_postings` calls are serialized under one
  `threading.Lock`; no two threads write `jobs.db` simultaneously.
- **Result integrity (FR-008)**: postings stored under concurrency must equal those stored
  sequentially for the same stubbed inputs — no loss, duplication, or corruption; dedupe
  fingerprint behavior is unchanged.
- **Attributable logging (FR-008)**: each board's per-source log line stays intact; log
  lines may interleave across boards but each remains a complete, single-board line.
- **Failure containment (FR-008)**: one board raising or hanging affects only its own
  future; other boards complete. Same containment as the sequential `try/except` today.
