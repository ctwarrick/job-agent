# Quickstart: LLM Scoring Spend Efficiency

**Feature**: `002-scoring-spend-efficiency` | **Date**: 2026-06-12

Validation scenarios that exercise the feature end-to-end against a **stubbed**
Anthropic client (no network, no spend) — the pattern in
[tests/test_score.py](../../tests/test_score.py). Each scenario maps to user
stories and success criteria and is the concrete behavior the TDD tests assert.
The pure filter (US1 gate logic) is additionally unit-tested with **zero**
stubbing in `tests/test_filter.py`.

## Setup (all scenarios)

```bash
uv sync
# Provide a filter.toml in the data dir (copy the committed example and tune):
cp filter.toml.example filter.toml     # git-ignored; for local validation only
export ANTHROPIC_API_KEY=stub JOBAGENT_SALARY_FLOOR=120000
```

Tests stub `Anthropic` so `messages.create` returns a canned JSON array plus a
fake `usage` object (with `input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens`), and run against a
temp `jobs.db` seeded via `store.upsert_postings`.

## Scenario 1 — Filter before you spend (US1 · FR-001/003 · SC-001/004/006)

1. Seed a mixed set: relevant engineering roles, denylisted-function roles
   (titles containing `sales`, `accountant`, `recruiter`), stale roles
   (`posted_at` > 30 days), in-region suburbs (`"Renton, WA"`,
   `"King of Prussia, PA"`), clearly out-of-region roles (`"Austin, TX"`), a
   `"Washington, DC"` decoy, and rows with missing `posted_at` / `"Unspecified"`
   location.
2. Run the score stage.

**Assert**:
- Only plausible postings reach the stubbed LLM (capture the fingerprints passed
  to `messages.create`); denylisted/stale/out-of-region ones are absent.
- Rejected rows have a non-NULL `postings.filter_reason` with the expected
  machine-readable value (`function_denylist:sales`, `age:<n>d`,
  `location:<value>`).
- Rows with missing `posted_at` or `"Unspecified"` location **fail open** —
  they reach the LLM, not rejected (SC-004).
- **Region-token location matching**: `"Renton, WA"` and `"King of Prussia, PA"`
  reach the LLM (state token in `regions`); `"Austin, TX"` is rejected
  `location:Austin, TX`; the `"Washington, DC"` decoy is **rejected** (token is
  `DC`, not `WA`) — confirming `regions` matches the comma token, not a raw
  substring.
- The advisory allowlist never rejects: an off-allowlist-but-not-denylisted role
  still reaches the LLM.
- **Re-run** the score stage: no `filter.toml`/LLM re-evaluation of already-
  rejected or already-scored rows (`messages.create` not called for them) —
  SC-006.

## Scenario 2 — Per-run budget cap, posting count (US2 · FR-005/006/007/008)

1. Seed a post-filter backlog of, say, 25 plausible postings.
2. Set `JOBAGENT_MAX_POSTINGS_PER_RUN=10`, high cost cap. Run.

**Assert**:
- Exactly 10 postings scored (final batch trimmed to honor posting granularity,
  not rounded up to a whole `BATCH` — spec edge case), 15 remain scorable.
- `SCORE_CAP_STOP reason=postings …` and `SCORE_SUMMARY … remaining=15 …` logged.
- Process returns normally (exit 0) — a cap stop is not a failure.
- Subsequent runs **resume** the remaining backlog at the same per-run cap (a
  second run scores the next 10, leaving 5; a third drains them), re-scoring
  none of the already-scored rows — FR-007.

## Scenario 3 — Per-run budget cap, dollar cap (US2 · FR-005 · SC-002)

1. Seed plausible postings; set a very low `JOBAGENT_MAX_COST_PER_RUN` (e.g.
   `0.01`) so the projected cost crosses after the first batch.

**Assert**:
- The run stops **before** issuing the batch that would cross the cap (pre-call
  projection — research §5), so the summary's `est_cost_usd` ≤ the cap (SC-002).
- `SCORE_CAP_STOP reason=cost …` logged; exit 0.

## Scenario 4 — Cache the static prefix (US3 · FR-009/010)

1. Seed enough plausible postings for ≥ 2 batches. Run.

**Assert**:
- Every `messages.create` call carries the profile + screening prompt in a
  `system` block marked `cache_control: {"type": "ephemeral"}`, and the postings
  in the `user` turn (inspect the recorded call kwargs).
- Summed `usage`: `cache_read_input_tokens > 0` for batches after the first
  (stub returns cache-read on calls 2..N), reflected in `SCORE_SUMMARY`
  `cache_read_tokens` — FR-009.
- FR-010 (content change → fresh content): a unit-level assertion that the cached
  `system` content equals the current `profile.md` + `screening_prompt.md` bytes,
  so a changed file changes the cached block (and thus the cache key). No stale
  prefix is constructed across a content change.

## Scenario 5 — Cost observability (US4 · FR-011 · SC-005)

1. Run any scenario with a mix of filtered and scored postings.

**Assert**: exactly one `SCORE_SUMMARY` line reports `fetched`, `filtered` with a
per-reason breakdown (`function_denylist:`, `age:`, `location:`), `scored`,
`remaining`, the four token totals, and `est_cost_usd` — all derivable from logs
without re-running (SC-005). No posting content / rationale appears in the line.

## Scenario 6 — Empty post-filter set (edge case)

1. Seed only denylisted-function postings. Run.

**Assert**:
- `messages.create` is **never called** (zero LLM calls, zero spend).
- `SCORE_SUMMARY … scored=0 …` logged; exit 0; not an error (spec edge case).

## Scenario 7 — Fail loud on bad config (FR-014 · Principle V)

1. With a malformed `filter.toml` (or `JOBAGENT_MAX_COST_PER_RUN=abc`), run.

**Assert**: the stage `sys.exit`s non-zero **before** any `messages.create` call;
no partial external effect; the message names the offending file/var.

---

### Mapping

| Scenario | User story | Key FR / SC |
|---|---|---|
| 1 | US1 filter | FR-001/002/003/004 · SC-001/004/006 |
| 2 | US2 cap (count) | FR-005/006/007/008 |
| 3 | US2 cap (cost) | FR-005 · SC-002 |
| 4 | US3 caching | FR-009/010 |
| 5 | US4 observability | FR-011 · SC-005 |
| 6 | edge: empty | FR-001 (zero-call) |
| 7 | fail-loud | FR-014 · Principle V |
