# Quickstart / Validation: Resilient, Time-Bounded ATS Fetching

Stub-based, no network. Run the suite and the targeted scenarios below to prove
each user story. See [contracts/resilient-fetch.md](contracts/resilient-fetch.md)
and [data-model.md](data-model.md) for the contracts referenced here.

## Prerequisites

```bash
uv sync
uv run pytest        # full suite must be green (reviewer re-runs this)
```

## US1 — Digest still arrives for a very large board (P1)

Goal: a ~900-posting board does not exhaust the window; expensive detail fetches
scale with **filter survivors**, not board size (SC-001/SC-004).

- `tests/test_resilient.py`: stub `list_postings` returning ~900 listing stubs
  where the filter rejects most; assert `fetch_description` is called only for the
  survivor count, and `run_source` returns within the budget (fake clock).
- `tests/test_workday.py`: a 900-stub board with a small survivor set issues one
  detail GET per survivor only.

Expected: detail-call count == survivor count; no per-employer cap involved;
`SourceResult.new` == survivors described.

## US2 — One bad posting does not zero out a board (P2)

Goal: a transient per-item failure skips one item, keeps the rest (SC-003).

- Nth `fetch_description` raises ⇒ that posting absent, all others stored,
  `skipped == 1`, run continues.
- A listing page raises mid-pagination ⇒ earlier pages' postings retained; if every
  page fails ⇒ wholly-failed source path (existing per-source containment, FR-006).

## US3 — Every affected adapter bounded, including the dark one (P3)

Goal: Workday, iCIMS, **and** the dark Talemetry adapter all demonstrate bounded
request volume + per-item resilience under stubs (SC-006); Greenhouse/Lever
unchanged (FR-008).

```bash
uv run pytest tests/test_workday.py tests/test_icims.py tests/test_talemetry.py
uv run pytest tests/test_resilient.py
```

- Backstop: set `JOBAGENT_MAX_DETAIL_PER_SOURCE=5` against a 20-survivor stub ⇒
  exactly 5 detail calls, `truncated=True`, `remaining=15`.
- Forward progress: pre-seed the store with 5 survivors' external_ids ⇒ next run
  describes the other 15 (no re-describe of the first 5); two runs cover all 20.
- iCIMS inline-description short-circuit: `fetch_description` is never called when
  the stub already carries a description (assert call count 0).
- Greenhouse/Lever: `uv run pytest tests/test_greenhouse.py tests/test_lever.py`
  (if present) unchanged; their adapters are untouched.

## US4 — Accurate "new postings" count (P4)

```bash
uv run pytest tests/test_store.py -k upsert
```

- Upsert K brand-new postings ⇒ return value `== K` (not `2K`).
- Upsert when all already exist ⇒ return `0`.

## Bounded-staleness alert (FR-015 / SC-008 / SC-010)

- `last_converged_at` older than `JOBAGENT_STALENESS_BOUND_DAYS` with
  `remaining > 0` ⇒ `SourceResult.persistent == True`, and the digest renders the
  persistent-degradation message.
- A run that drains the last survivors (`remaining == 0`) ⇒ `mark_converged`
  called; a later truncation starts a fresh grace window.

## End-to-end degraded digest (FR-014)

```bash
DIGEST_DRY_RUN=1 uv run python main.py   # with a stubbed registry/clock in tests
```

- `tests/test_digest.py`: a `partial_sources` entry renders a "partial / degraded"
  block distinct from the "unreachable" (failed-source) block and from a clean run.

## Manual smoke (optional, real network — NOT part of CI)

Not required for done. If validating a live Workday board, set a low
`JOBAGENT_MAX_DETAIL_PER_SOURCE` and `JOBAGENT_FETCH_DEADLINE_SECONDS`, run
`uv run jobagent-fetch`, and confirm the log reports survivors-described and any
truncation. Never commit `registry.toml`.
