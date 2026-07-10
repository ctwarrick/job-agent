# Contract: Fetch Stage (concurrency + budget)

Governs `fetch.main()`. Preserves the existing `(failed_sources, partial_sources)` return
shape and all 006 per-source guarantees.

## Dispatch order

Boards are dispatched in `last_converged_at ASC NULLS FIRST` order (never-fetched and
longest-stale first — FR-006). Ordering is computed once per run from `source_progress`.

## Concurrency (FR-007, FR-008)

- Up to `JOBAGENT_FETCH_CONCURRENCY` boards fetch in parallel via a `ThreadPoolExecutor`.
- `concurrency == 1` MUST reproduce today's sequential behavior and ordering exactly.
- Each board's `upsert_postings` is serialized under a single lock (one SQLite writer).
- Invariants under concurrency (must hold identically to sequential for stubbed inputs):
  - **No lost/dup/corrupt postings**; dedupe fingerprint unchanged.
  - **Failure containment**: one board raising/hanging affects only its own result; every
    other board completes and is recorded.
  - **Attributable logs**: each per-source summary line stays a complete single-board line.

## Stage budget (FR-003, FR-009)

- `stage_deadline = clock() + JOBAGENT_FETCH_BUDGET_SECONDS`, computed once at fetch start.
- **Submission stop**: once `clock() > stage_deadline`, no new board is submitted.
- **In-flight clamp**: a dispatched board's effective deadline is
  `min(per_source_deadline, stage_deadline)`, so a board submitted just before the deadline
  cannot run its full per-source budget past the stage stop.
- **Clean stop**: everything fetched before the stop is retained; the run proceeds to
  score/digest. No thread is hard-cancelled (avoids torn writes).
- **Composition**: per-source cap/deadline bound each board; the stage budget bounds their
  sum; whichever is nearer fires first (FR-009). 006 semantics unchanged.

## Outcome reporting (FR-005)

- **Deferred**: boards never dispatched because the budget expired. Surfaced in the digest
  as a distinct degraded category with a count of deferred boards — separate from failed
  and per-source-partial. A deferred board does NOT advance its `last_converged_at`, so it
  sorts first next run.
- **Run outcome**: deferred boards → `degraded` (non-fatal); a digest is still sent and
  `RUN_SUCCESS` still prints (consistent with `main.py`'s existing degraded handling).

## Acceptance oracles (map to spec scenarios)

| Scenario | Oracle |
|---|---|
| US2-1 budget truncation | stubbed slow boards + tiny budget → fetch stops at budget, score/digest run, `RUN_SUCCESS` emitted |
| US2-2 digest reports deferral | digest body shows "N boards deferred" distinct category |
| US2-3 no starvation | deferred board sorts first next run; full coverage within bounded runs |
| US3-1 speedup | stubbed latency boards: wall-clock(concurrency=N) ≪ wall-clock(1); ≥3x |
| US3-2 containment | one hanging/failing board; others still stored |
| US3-3 integrity | stored postings identical to sequential run for same stubs |
