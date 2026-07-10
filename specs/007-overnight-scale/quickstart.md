# Quickstart: Validating Overnight Run Scaling

Stub-based, no network — follow the patterns in `tests/test_fetch.py` and
`tests/test_resilient.py`. All scenarios run under `uv run pytest`.

## Prerequisites

- `uv sync`
- No live registry / Azure needed; stubs provide boards and simulated latency.

## Scenario 1 — Concurrency equivalence & speedup (US3)

Stub N boards each sleeping a fixed simulated latency.
- Run `fetch.main()` with `JOBAGENT_FETCH_CONCURRENCY=1` and again with `=8`.
- **Expect**: identical stored postings and per-source outcomes; wall-clock at 8 is
  ≥3x faster than at 1 (SC-004). Assert on a monotonic-clock delta, not real sleeps where
  avoidable (inject `clock`).

## Scenario 2 — Failure containment under concurrency (US3-2)

One stubbed board raises / hangs; others succeed.
- **Expect**: the bad board appears in `failed`/`partial`; every other board's postings are
  stored; run continues.

## Scenario 3 — Stage budget clean stop (US2-1)

Many stubbed slow boards + `JOBAGENT_FETCH_BUDGET_SECONDS` small (inject `clock`).
- **Expect**: fetch stops submitting at the budget, retains everything already fetched,
  returns deferred boards, and the pipeline reaches `RUN_SUCCESS` (score/digest run).

## Scenario 4 — Deferred boards reported & prioritized (US2-2, US2-3)

- **Expect (report)**: `digest.main()` renders a distinct "N boards deferred" degraded
  category, separate from failed and per-source-partial.
- **Expect (no starvation)**: a board deferred in run A (timestamp not advanced) sorts
  first in run B; over bounded runs every board reaches a full fetch (SC-003).

## Scenario 5 — Startup validation fail-loud (US1 / FR-004)

Set `JOBAGENT_FETCH_BUDGET_SECONDS + JOBAGENT_SCORE_DIGEST_HEADROOM_SECONDS >
JOBAGENT_EXECUTION_WINDOW_SECONDS`.
- **Expect**: `main.main()` raises `SystemExit` (non-zero) naming all three values; nothing
  is fetched/scored/emailed. Follow `tests/test_main.py` fail-loud patterns.

## Scenario 6 — Infra compiles (US1)

- Run `scripts/validate-infra.sh`.
- **Expect**: `OK: infra compiles` with `replicaTimeoutSeconds=7200`, the DST-safe cron, and
  the `JOBAGENT_EXECUTION_WINDOW_SECONDS` env pass-through present.

## Cross-cutting

- 006 per-source tests (`test_resilient.py`) MUST stay green unchanged — proof that
  per-source semantics were preserved (FR-009).
- `concurrency=1` path is the regression oracle for every concurrent assertion.
