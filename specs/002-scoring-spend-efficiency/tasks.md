---
description: "Task list for LLM Scoring Spend Efficiency (feature 002)"
---

# Tasks: LLM Scoring Spend Efficiency

**Input**: Design documents from `/specs/002-scoring-spend-efficiency/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Tests**: INCLUDED. The constitution mandates Test-First Delivery (Principle III)
and the plan's Constitution Check authors failing tests for the filter, the cap
loop, the resume query, and the cost summary *before* implementation. Every
story writes its tests red first.

**Organization**: Tasks are grouped by user story (spec priorities P1–P3). Stories
US2–US4 all modify `score.py`'s `main()`/`_score_batch`, so they are **sequential
on that file** — they build on US1's refactor rather than running in parallel.
This is an intentional deviation from the template's "stories run in parallel"
default and is called out in Dependencies.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1–US4; Setup/Foundational/Polish carry no story label
- Exact file paths are in each task

## Path Conventions

Single Python project: `src/job_agent/`, `tests/` at repo root (per
[plan.md](plan.md) Structure Decision). No restructuring.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: committed schema reference + privacy gitignore, before any code.

- [ ] T001 [P] Create committed `filter.toml.example` at repo root with the generic, non-personal schema from [contracts/filter-criteria.md](contracts/filter-criteria.md) (`[denylist].title_keywords`, advisory `[allowlist].title_keywords`, `[age].max_days=30`, `[location]` with `remote_ok`, `regions`, `metros`)
- [ ] T002 [P] Add `filter.toml` to `.gitignore` (keep `filter.toml.example` tracked), beside the existing `profile.md`/`screening_prompt.md`/`registry.txt` entries

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: the `store.py` persistence spine for the posting scoring lifecycle
([data-model.md](data-model.md)). Blocks US1 (rejection persistence) and US2
(resume via `scorable()`).

**⚠️ CRITICAL**: No score-stage story can be completed until this phase is done.

- [ ] T003 [P] Write failing tests in `tests/test_store.py` for: (a) idempotent `filter_reason` migration via `_migrate` (column added once, re-run is a no-op, existing `skills_fit` untouched — invariant I4); (b) `scorable()` returns only rows where `skills_fit IS NULL AND filter_reason IS NULL`; (c) `record_filter_rejections([(fp, reason), …])` persists `filter_reason`. Run `uv run pytest tests/test_store.py` and confirm RED.
- [ ] T004 Add `filter_reason TEXT` to the `postings` `CREATE TABLE` in `DDL` and an idempotent `if "filter_reason" not in cols` guard in `_migrate()` in `src/job_agent/store.py` (mirror the `digest_sent_at` pattern)
- [ ] T005 Implement `scorable(path: str | None = None) -> list[sqlite3.Row]` in `src/job_agent/store.py` (`SELECT * FROM postings WHERE skills_fit IS NULL AND filter_reason IS NULL`); leave `unscored()` unchanged
- [ ] T006 Implement `record_filter_rejections(rejections, path: str | None = None) -> None` in `src/job_agent/store.py` (single-transaction `UPDATE postings SET filter_reason=? WHERE fingerprint=?` over the pairs)

**Checkpoint**: `uv run pytest tests/test_store.py` GREEN — the persistence spine is ready.

---

## Phase 3: User Story 1 - Filter before you spend (Priority: P1) 🎯 MVP

**Goal**: a deterministic, zero-cost gate drops obviously-irrelevant postings
before any LLM call; rejections persist with a reason and are never re-evaluated.

**Independent Test**: seed mixed postings (relevant + denylisted-function + stale
+ out-of-region + missing-field), run the score stage against a stubbed LLM, and
confirm only plausible postings reach the LLM, rejects carry the right
`filter_reason`, and a re-run re-examines none of them
([quickstart.md](quickstart.md) Scenario 1).

### Tests for User Story 1 (write FIRST, prove RED)

- [ ] T007 [P] [US1] Write failing pure-function tests in `tests/test_filter.py` (no stubbing) for `classify()`: denylist hard reject → `function_denylist:<kw>`; allowlist is advisory (off-allowlist non-denylisted posting is NOT rejected); age gate rejects `> max_days` but fails open on missing/unparseable `posted_at`; location region-token keep — `"Renton, WA"` & `"King of Prussia, PA"` kept, `"Austin, TX"` rejected `location:…`, `"Washington, DC"` decoy rejected (token `DC` ≠ `WA`), missing/`"Unspecified"` fails open; and `load_criteria` raising on malformed/missing TOML. Confirm RED.
- [ ] T008 [P] [US1] Write failing tests in `tests/test_score.py` for `score.main()` filter integration (stub `Anthropic` + `store` per existing patterns): only plausible postings reach the stubbed LLM; rejects persisted via `record_filter_rejections`; re-run scores nothing (uses `store.scorable()`, SC-006); and `sys.exit` before any `messages.create` when `filter.toml` is missing/malformed (FR-014). Confirm RED.

### Implementation for User Story 1

- [ ] T009 [P] [US1] Create `src/job_agent/filter.py`: a frozen `Criteria` dataclass, `load_criteria(path=None)` (stdlib `tomllib`; validate keyword lists, positive-int `max_days`, bool `remote_ok`; raise/`sys.exit` on malformed — research §4), and pure `classify(posting, criteria) -> str | None` implementing the gate order/semantics in [contracts/filter-criteria.md](contracts/filter-criteria.md) (denylist hard, allowlist advisory, age fail-open, location region-token keep)
- [ ] T010 [US1] Wire `src/job_agent/score.py` `main()`: load criteria (fail-loud before client init), run `classify` over `store.scorable()`, `store.record_filter_rejections()` the rejects, and send only the plausible set to scoring; switch the source query from `store.unscored()` to `store.scorable()`

**Checkpoint**: US1 fully functional — `uv run pytest tests/test_filter.py tests/test_score.py` GREEN; this is the MVP and the dominant cost lever (SC-001).

---

## Phase 4: User Story 2 - Per-run budget guardrail (Priority: P2)

**Goal**: a configurable cap (200 postings AND $5 estimated, whichever first)
stops a run loudly; progress persists; the next run resumes.

**Independent Test**: set a low cap against a backlog larger than it, confirm the
run stops at the cap, logs `SCORE_CAP_STOP`, exits 0, and a second run resumes the
remainder ([quickstart.md](quickstart.md) Scenarios 2–3).

### Tests for User Story 2 (write FIRST, prove RED)

- [ ] T011 [P] [US2] Write failing tests in `tests/test_score.py`: posting cap trims the final batch to honor granularity (exactly N scored, remainder still scorable); a second run resumes the remainder (FR-007); dollar cap stops *before* the batch that would cross it (pre-call projection — `est_cost ≤ cap`, SC-002); `SCORE_CAP_STOP reason=postings|cost …` logged; process returns normally (exit 0); and `sys.exit` on invalid cap/price env before any LLM call (FR-014). Confirm RED.

### Implementation for User Story 2

- [ ] T012 [US2] In `src/job_agent/score.py`, read + fail-loud-validate `JOBAGENT_MAX_POSTINGS_PER_RUN` (200) and `JOBAGENT_MAX_COST_PER_RUN` (5.00) and the `JOBAGENT_PRICE_*` defaults (research §7); add a cost helper (per-posting projected cost for the cap + `usage`→$ for later) per [contracts/runtime-config.md](contracts/runtime-config.md)
- [ ] T013 [US2] Implement the cap loop in `score.py` `main()`: stop at the posting cap (trim final batch) or the projected dollar cap (pre-call check), emit `SCORE_CAP_STOP`, return normally (exit 0), preserving the existing incremental per-batch commit (FR-006/007/008)

**Checkpoint**: US1+US2 work — worst-case run spend is bounded (SC-002); cap stop is a clean, resumable exit 0.

---

## Phase 5: User Story 3 - Cache the static prefix (Priority: P3)

**Goal**: the profile + screening prompt are sent as a cached `system` block so
they are billed once per run, not once per batch.

**Independent Test**: run ≥ 2 batches and confirm every request carries the
static prefix with `cache_control` and that cache reuse shows up in usage
([quickstart.md](quickstart.md) Scenario 4).

### Tests for User Story 3 (write FIRST, prove RED)

- [ ] T014 [P] [US3] Write failing tests in `tests/test_score.py`: `_score_batch` puts profile + screening prompt in a `system` block marked `cache_control: {"type": "ephemeral"}` with only postings in the `user` turn; summed `cache_read_input_tokens > 0` after the first batch (FR-009); and the cached `system` content equals the current `profile.md` + `screening_prompt.md` bytes so a content change is reflected next run (FR-010). Confirm RED.

### Implementation for User Story 3

- [ ] T015 [US3] Refactor `_score_batch` (and `PROMPT_TEMPLATE`) in `src/job_agent/score.py` to move the profile + screening prompt into a single cached `system` block (`cache_control` ephemeral) and keep only the per-batch postings block in the `user` message (research §6)

**Checkpoint**: US1–US3 work — per-batch input cost for scored postings drops via cache reuse.

---

## Phase 6: User Story 4 - Cost observability (Priority: P3)

**Goal**: one run-summary log line reports counts, token usage, and estimated
cost — readable without re-running or waiting for the bill.

**Independent Test**: run a mixed filtered/scored set and confirm exactly one
`SCORE_SUMMARY` line carries the counts, four token totals, and `est_cost_usd`
([quickstart.md](quickstart.md) Scenario 5).

### Tests for User Story 4 (write FIRST, prove RED)

- [ ] T016 [P] [US4] Write failing tests in `tests/test_score.py`: exactly one `SCORE_SUMMARY` line with `fetched`, `filtered` + per-reason breakdown (`function_denylist:`/`age:`/`location:`), `scored`, `remaining`, the four token totals, and `est_cost_usd`; no posting content/rationale in the line (Principle VI); and an empty post-filter set still emits `scored=0` with zero LLM calls (edge case). Confirm RED.

### Implementation for User Story 4

- [ ] T017 [US4] Accumulate `usage` across batches and emit one `SCORE_SUMMARY` line (counts + reason breakdown + token totals + estimated cost via the T012 cost helper) in `src/job_agent/score.py` `main()` (FR-011, SC-005)

**Checkpoint**: All four stories complete — filter, cap, cache, and observability.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T018 [P] Update `README.md` runtime-files / "deploy your own" list to include `filter.toml` (private, Azure Files upload) and note the committed `filter.toml.example`
- [ ] T019 Run `uv run pytest` (full suite GREEN — the quality gate the reviewer re-runs) and apply Black (line length 100) to `src/job_agent/filter.py`, `src/job_agent/score.py`, `src/job_agent/store.py`, and the touched tests
- [ ] T020 [P] Walk the [quickstart.md](quickstart.md) Scenarios 1–7 against a stubbed run to validate the acceptance criteria end-to-end
- [ ] T021 [P] (Optional) Declare `JOBAGENT_MAX_POSTINGS_PER_RUN` / `JOBAGENT_MAX_COST_PER_RUN` / `JOBAGENT_PRICE_*` as plain env entries in `infra/main.bicep` for prod visibility (defaults already make this non-blocking — FR-008)

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (P1 tasks)**: no dependencies — start immediately.
- **Foundational (Phase 2)**: depends on Setup; **blocks US1 and US2** (rejection persistence + `scorable()` resume).
- **User stories (Phases 3–6)**: each depends on Foundational. Because US2–US4 all edit `score.py` `main()`/`_score_batch`, they are **sequential** in priority order (US1 → US2 → US3 → US4), not parallel.
- **Polish (Phase 7)**: depends on all desired stories.

### Story dependencies

- **US1 (P1)**: needs Foundational. The MVP; establishes the filter call site in `score.main()`.
- **US2 (P2)**: needs Foundational (`scorable()` resume) and builds on US1's refactored `main()`.
- **US3 (P3)**: builds on US1's `main()`; modifies `_score_batch`.
- **US4 (P3)**: reuses US2's cost helper; builds on US1's `main()`.

### Within each story

- Tests authored and RED before implementation (Principle III).
- store layer (Phase 2) before any score-stage story.
- `filter.py` (T009) before its `score.py` wiring (T010).

### Parallel opportunities

- T001 ∥ T002 (different files).
- T007 ∥ T008 (different test files: `test_filter.py` vs `test_score.py`); T009 (new `filter.py`) ∥ those test tasks.
- Across stories, the **test-authoring** tasks (T011/T014/T016) can be drafted in parallel, but their `score.py` **implementations** (T013/T015/T017) must land sequentially (same file).
- Polish T018 ∥ T020 ∥ T021.

---

## Parallel Example: User Story 1

```bash
# Author both red test files together (different files):
Task: "T007 pure-function gate tests in tests/test_filter.py"
Task: "T008 filter-integration tests in tests/test_score.py"

# Then the new pure module can be built alongside finalizing tests:
Task: "T009 create src/job_agent/filter.py (Criteria, load_criteria, classify)"
# T010 (score.py wiring) follows, depending on T009.
```

---

## Implementation Strategy

### MVP first (User Story 1 only)

1. Phase 1 Setup → 2. Phase 2 Foundational (store spine) → 3. Phase 3 US1.
4. **STOP and validate**: quickstart Scenario 1 — the filter alone delivers the
   bulk of the savings (SC-001) and is independently shippable.

### Incremental delivery

US1 (filter, MVP) → US2 (cap: worst-case bound) → US3 (cache: per-batch savings)
→ US4 (observability). Each is a complete, independently testable increment that
adds value without breaking the prior ones; `uv run pytest` stays green at every
checkpoint.

---

## Notes

- [P] = different files, no dependency on an incomplete task.
- `score.py`'s `main()`/`_score_batch` are the shared hot spot — sequence the
  per-story implementations there; only the test files parallelize cleanly.
- Verify each story's tests fail before implementing (TDD gate).
- Nothing is committed/pushed without the human gate (AGENTS.md).
