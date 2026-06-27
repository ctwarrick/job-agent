---
description: "Task list for Resilient, Time-Bounded ATS Fetching"
---

# Tasks: Resilient, Time-Bounded ATS Fetching

**Input**: Design documents from `specs/006-resilient-fetch/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md,
contracts/resilient-fetch.md, quickstart.md (all present)

**Tests**: REQUIRED. FR-012 and Constitution III mandate test-first; every story
authors failing stub-based tests (no network) and observes them red before any
implementation code is written.

**Organization**: Grouped by user story. This feature is a shared-contract
refactor (FR-007): the core `resilient.run_source` built in US1 is the MVP, and
US2/US3 extend that same contract. US4 is fully independent. See Dependencies.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on incomplete work)
- **[Story]**: US1–US4; Setup/Foundational/Polish carry no story label
- Exact file paths are in each task

## Path Conventions

Single project: `src/job_agent/` and `tests/` at repo root.

---

## Phase 1: Setup (baseline)

**Purpose**: Establish a known-green starting point and protect out-of-scope work.

- [X] T001 Baseline established: `uv run pytest` green (181 passed). Note: there
      are **no** dedicated `test_greenhouse.py` / `test_lever.py` files — those
      adapters are exercised only indirectly (`test_fetch.py`, `test_main.py`).
      FR-008 is therefore enforced by leaving `greenhouse.py` / `lever.py` and
      their plain-`fetch` dispatch **byte-unchanged**, not by a dedicated suite.

---

## Phase 2: Foundational (store layer) — BLOCKS US1/US2/US3

**Purpose**: Forward-progress and staleness persistence that `resilient.run_source`
depends on. Additive only — no destructive migration (FR-009).

**⚠️ CRITICAL**: US1/US2/US3 cannot begin until this phase is complete.

- [X] T002 [P] Write failing tests in `tests/test_store.py` for: (a)
      `existing_external_ids(source, company)` returning the set of stored
      external_ids for a source (empty set when none); (b) `source_progress`
      helpers `get_last_converged` / `mark_converged` (upsert) / `seed_source`
      (insert-if-absent); (c) `init()` creating `source_progress` idempotently
      with existing postings/scores/applications rows preserved (FR-009).
- [X] T003 Add `source_progress(source, company, last_converged_at)` table to
      `store.init()` as an idempotent `CREATE TABLE IF NOT EXISTS` (additive
      migration, PK `(source, company)`) in `src/job_agent/store.py`.
- [X] T004 Implement `existing_external_ids(source, company, path=None)` in
      `src/job_agent/store.py` (`SELECT external_id FROM postings WHERE
      source=? AND company=?` → `set[str]`).
- [X] T005 Implement `get_last_converged` / `mark_converged` / `seed_source`
      `(source, company, ...)` in `src/job_agent/store.py`.

**Checkpoint**: forward-progress and staleness state persist; store tests green.

---

## Phase 3: User Story 1 - Digest arrives for a very large board (P1) 🎯 MVP

**Goal**: List cheaply, filter on listing-level fields, fetch the expensive
description only for survivors, bound each source with a cap+deadline backstop,
and make monotonic forward progress so a huge board converges across runs — so
fetch fits the window and the digest is delivered (FR-001/002/003/004/013/015).

**Independent Test**: A stubbed ~900-posting board where the filter rejects most
→ `fetch_description` is called only for the survivor count (SC-004);
`run_source` returns within a fake-clock budget; with a small cap it reports
`truncated=True`, `remaining>0`; pre-seeding stored external_ids makes the next
run describe only the rest (two runs cover all, no re-described prefix).

### Tests for User Story 1 (write first, observe red)

- [X] T006 [P] [US1] Write failing tests in `tests/test_resilient.py` covering:
      backstop config defaults (150/300/7) and fail-loud on invalid values;
      filter-before-detail (rejected stubs trigger **zero** `fetch_description`
      calls; detail-call count == survivor count, SC-004); backstop (cap ⇒
      exactly K detail calls, fake monotonic clock past deadline stops mid-list,
      `truncated=True`, `remaining>0`); forward progress (pre-seeded external_ids
      skipped; two-run full coverage with no re-described prefix); staleness
      (`last_converged` older than bound + `remaining>0` ⇒ `persistent=True`;
      `remaining==0` ⇒ `mark_converged` called, `persistent=False`; new source
      seeds `last_converged=now`).
- [X] T007 [P] [US1] Write failing tests in `tests/test_workday.py` for the
      two-phase shape: `list_postings` paginates the listing only (no detail
      calls) returning stubs with `description=""`; `fetch_description` does the
      single detail GET; a large stub board with a small survivor set issues one
      detail GET per survivor; the `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER`
      per-employer cap is gone (full long tail listed). Monkeypatch
      `workday.requests` / `workday.time.sleep` per the existing pattern.

### Implementation for User Story 1

- [X] T008 [US1] Create `src/job_agent/resilient.py` with the backstop config
      readers (`JOBAGENT_MAX_DETAIL_PER_SOURCE`=150, `JOBAGENT_FETCH_DEADLINE_
      SECONDS`=300, `JOBAGENT_STALENESS_BOUND_DAYS`=7; env-read, fail-loud on
      non-int/≤0) and the `SourceResult` dataclass (`source, company_slug, new,
      skipped, truncated, remaining, persistent, error`).
- [X] T009 [US1] Implement `resilient.run_source(adapter, source, *, criteria,
      store_, clock=time.monotonic, now=...)` in `src/job_agent/resilient.py`:
      `list_postings` → listing-level `classify(dict_view(stub), criteria)`
      filter (FR-003/004) → skip `existing_external_ids` (forward progress) →
      cap+deadline backstop loop → `desc = s.description or
      adapter.fetch_description(s)` (inline short-circuit) → `replace(s,
      description=desc)` → `store_.upsert_postings(described)` →
      `mark_converged`/`seed_source`/`persistent` bookkeeping → return
      `SourceResult(new=len(described), ...)`. (depends on T008, Phase 2)
- [X] T010 [P] [US1] Refactor `src/job_agent/adapters/workday.py` into
      `list_postings(slug, *, company=None, timeout=20)` (paginate listing,
      `external_id=externalPath`, `description=""`) and `fetch_description(
      posting, *, timeout=20)` (the `GET .../{externalPath}` detail call);
      remove the `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` per-employer cap.
- [X] T011 [US1] Route Workday through `resilient.run_source` in
      `src/job_agent/fetch.py`: `ADAPTERS` maps in-scope vendors to the
      two-phase module and dispatches them via `run_source`; out-of-scope
      vendors keep their plain `fetch`. Aggregate the returned `SourceResult`
      into the per-source log line and total new count. (depends on T009, T010)

**Checkpoint**: the live large board is bounded, filtered, and converges across
runs — the production incident is fixed for Workday; `test_resilient.py` and
`test_workday.py` green.

---

## Phase 4: User Story 2 - One bad posting doesn't zero a board + visible degradation (P2)

**Goal**: A single failed detail or listing page is logged and skipped while the
rest of the board is kept (FR-005/006/010), and partial degradation is surfaced
in the digest distinct from healthy and wholly-failed sources (FR-014).

**Independent Test**: Nth `fetch_description` raises ⇒ that posting absent,
others stored, `skipped==1`, run continues; a listing page raises mid-pagination
⇒ earlier pages retained; a wholly-failed source ⇒ `error` set and contained;
a `partial_sources` entry renders a degraded block in the digest.

### Tests for User Story 2 (write first, observe red)

- [X] T012 [P] [US2] Add failing tests in `tests/test_resilient.py`: Nth
      `fetch_description` raises ⇒ skipped+1, that posting unstored, others
      stored, run continues (FR-005); whole `list_postings` raises ⇒
      `SourceResult.error` set, no postings (FR-006 containment).
- [X] T013 [P] [US2] Add failing tests in `tests/test_workday.py`: a listing
      page failure mid-pagination retains earlier pages and continues; a
      survivor whose `fetch_description` fails is left unstored and unscored
      (FR-010), never scored on empty text.
- [X] T014 [P] [US2] Write failing tests in `tests/test_fetch.py`: `fetch.main`
      returns `(failed_sources, partial_sources)`; a source with `skipped>0` or
      `truncated` or `persistent` appears in `partial_sources` with
      `{source, company_slug, new, skipped, truncated, persistent}`.
- [X] T015 [P] [US2] Write failing tests in `tests/test_digest.py`: a
      `partial_sources` entry renders a "partial / degraded" block distinct from
      "unreachable" (failed) and from a healthy run; truncated-within-bound vs
      persistent-beyond-bound messages differ; no raw adapter error text appears
      (Principle VI).
- [X] T016 [US2] Add per-item `try/except` skip-not-abort (increment `skipped`,
      log, continue; leave failed-description survivors unstored) to the
      `run_source` detail loop in `src/job_agent/resilient.py`.
- [X] T017 [P] [US2] Add per-page skip-not-abort to `workday.list_postings`
      (log + skip a failed page, retain earlier pages, raise only on
      whole-source failure) in `src/job_agent/adapters/workday.py`.
- [X] T018 [US2] Aggregate `SourceResult`s in `fetch.main` into
      `(failed_sources, partial_sources)` and pass `partial_sources` to the
      digest in `src/job_agent/fetch.py`.
- [X] T019 [P] [US2] Extend `digest._degradation_facts` / `_messages` with the
      "partial / degraded" category (truncated vs persistent wording, no raw
      error text) in `src/job_agent/digest.py`.
- [X] T020 [US2] Wire `partial_sources` through the fetch→digest call site in
      `main.py` if the signature changed. (depends on T018)

**Checkpoint**: a bad item/page no longer zeroes a board, and partial
degradation is visible in the digest; US2 tests green, US1 still green.

---

## Phase 5: User Story 3 - Every adapter bounded, including the dark one (P3)

**Goal**: Extend the two-phase contract to iCIMS and the dark Talemetry adapter
so all three in-scope adapters are bounded + resilient under stubs (FR-007,
SC-006); Greenhouse/Lever stay unchanged (FR-008).

**Independent Test**: `uv run pytest tests/test_workday.py tests/test_icims.py
tests/test_talemetry.py` — each demonstrates bounded request volume and per-item
resilience under stubs, including dark Talemetry; iCIMS `fetch_description` makes
no second call when the stub already carries an inline description (call count
0); Greenhouse/Lever tests unchanged and green.

### Tests for User Story 3 (write first, observe red)

- [X] T021 [P] [US3] Write failing tests in `tests/test_icims.py`:
      `list_postings` returns stubs carrying the inline description
      (`external_id=req_id`); `fetch_description` is a pass-through that makes
      **no** second request (assert call count 0) when description is present;
      per-page resilience; per-employer cap removed; an empty inline description
      is excluded from scoring and not stored as an empty-description row.
- [X] T022 [P] [US3] Write failing tests in `tests/test_talemetry.py`:
      `list_postings` paginates job cards (title/location/date, `description=""`,
      numeric `external_id`); `fetch_description` does the detail GET + selector
      parse; under `run_source` the dark adapter is bounded (backstop) and
      per-item resilient (SC-006).
- [X] T023 [US3] Refactor `src/job_agent/adapters/icims.py`: `list_postings`
      returns inline-description stubs; `fetch_description` is a pass-through;
      add per-page skip-not-abort; remove the per-employer cap; keep excluding
      empty-inline-description postings (do not store empty-description rows).
- [X] T024 [P] [US3] Refactor `src/job_agent/adapters/talemetry.py` into
      `list_postings` (card pagination) + `fetch_description` (detail GET +
      parse); add per-page skip-not-abort; remove the per-employer cap.
- [X] T025 [US3] Add iCIMS and Talemetry to the in-scope `ADAPTERS` set routed
      through `resilient.run_source` in `src/job_agent/fetch.py` (in-scope =
      Workday + iCIMS + Talemetry). (depends on T023, T024)
- [X] T026 [US3] Confirm FR-008: `git diff --stat src/job_agent/adapters/
      greenhouse.py src/job_agent/adapters/lever.py` is empty (those adapters and
      their plain-`fetch` dispatch are untouched) and the full `uv run pytest`
      suite — including `test_fetch.py` / `test_main.py` that exercise them — is
      green. There are no dedicated greenhouse/lever test files.

**Checkpoint**: all three in-scope adapters bounded + resilient incl. dark
Talemetry; out-of-scope adapters untouched; full suite green.

---

## Phase 6: User Story 4 - Accurate "new postings" count (P4)

**Goal**: `store.upsert_postings` reports only the postings-insert count, not the
companion applications insert (FR-011). Independent — touches `store.py` +
`test_store.py` only; can be done at any point (even first).

**Independent Test**: Upsert K brand-new postings ⇒ return value `== K` (not
`2K`); upsert when all already exist ⇒ return `0`.

- [X] T027 [P] [US4] Write failing tests in `tests/test_store.py`: upsert K
      brand-new postings returns K (not 2K); upsert when all exist returns 0.
- [X] T028 [US4] Fix `store.upsert_postings` to return the postings-insert delta
      alone — snapshot `conn.total_changes` between the postings `executemany`
      and the applications `executemany` — in `src/job_agent/store.py`.

**Checkpoint**: fetch log "new" count is accurate (SC-007).

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T029 [P] Make the registry `max_per_employer` key accepted-but-ignored
      (documented deprecated, no fail-loud on load) in
      `src/job_agent/registry.py`.
- [X] T030 Run the `quickstart.md` validation: full `uv run pytest` green plus
      the per-story targeted scenarios (US1–US4, staleness, degraded digest).
- [X] T031 [P] README/docs drift check for the new env vars and fetch behavior
      (`README.md`). CHANGELOG/version bump are deferred to the release phase.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (P1)**: none — start immediately.
- **Foundational (P2)**: after Setup — **blocks US1/US2/US3** (`run_source`
  calls the new store functions).
- **US1 (Phase 3)**: after Foundational. Builds the shared `resilient.py` core
  (MVP).
- **US2 (Phase 4)**: after US1 — extends `run_source` (skip-not-abort) and adds
  digest visibility.
- **US3 (Phase 5)**: after US1 — reuses `run_source` for two more adapters.
  Independent of US2.
- **US4 (Phase 6)**: independent of everything — only `store.upsert_postings` +
  `test_store.py`. Can run first, last, or in parallel.
- **Polish (Phase 7)**: after US1–US3.

### Honest note on story independence

Unlike a typical vertical-slice feature, US2 and US3 **depend on US1's
`resilient.py`** (the shared contract, FR-007) — they are extensions, not
independent slices. US1 is the genuine MVP. US4 is the only fully independent
story.

### Within each story

- Test tasks are authored and observed **red** before implementation (FR-012).
- In US1: `resilient.py` config+dataclass (T008) before `run_source` (T009);
  adapter refactor (T010) can proceed in parallel; fetch wiring (T011) last.
- `store.py` tasks (T003–T005, T028) touch one file — keep sequential.

### Parallel opportunities

- **Cross-file test authoring** within a story runs in parallel: US2's T012–T015
  (resilient/workday/fetch/digest test files); US3's T021–T022.
- **Cross-file implementation**: US1 T010 (workday) ∥ T008/T009 (resilient);
  US2 T017 (workday) ∥ T019 (digest); US3 T024 (talemetry) ∥ T023 (icims).
- **US4** can run in parallel with any phase (disjoint files).
- Same-file tasks are **not** parallel: all `store.py` impl tasks; T008→T009 and
  T009→T016 in `resilient.py`; T011→T025 in `fetch.py`.

---

## Parallel Example: User Story 2 test authoring

```bash
# Author all US2 failing tests together (distinct files):
Task: "Per-item/page resilience tests in tests/test_resilient.py"   # T012
Task: "Page-failure + failed-description tests in tests/test_workday.py"  # T013
Task: "fetch.main partial_sources tests in tests/test_fetch.py"     # T014
Task: "Digest partial/degraded rendering tests in tests/test_digest.py"  # T015
```

---

## Implementation Strategy

### MVP first (US1)

1. Phase 1 Setup → 2. Phase 2 Foundational (store) → 3. Phase 3 US1.
4. **STOP and VALIDATE**: the stubbed ~900-board test fits the window, detail
   calls scale with survivors, and the board converges across runs. The
   production incident is resolved for the live adapter at this point.

### Incremental delivery

1. Foundational → US1 (MVP, live board fixed) → validate.
2. US2 (per-item resilience + visible degradation) → validate.
3. US3 (iCIMS + dark Talemetry bounded; Greenhouse/Lever unchanged) → validate.
4. US4 (count fix) — slot in anywhere; it is disjoint.

### Suggested quick win

US4 (T027–T028) is tiny and independent; doing it early gives an accurate
"new" count for diagnosing the rest of the build, at no coupling cost.

---

## Notes

- Tests are stub-based, no network: monkeypatch `adapter.requests` /
  `adapter.time.sleep`; drive the deadline with a fake monotonic `clock`; use
  `:memory:` / `tmp_path` DBs and an injectable `now` for staleness.
- `schema.py` and the fingerprint scheme are unchanged: stubs are **never**
  stored description-less; a survivor is upserted only after its description is
  fetched (D3/R3), so identity is final at insert (FR-009).
- No infra touched → `scripts/validate-infra.sh` is N/A; the reviewer re-runs
  `uv run pytest` only.
- Never quote `registry.toml` / `profile.md` / `screening_prompt.md` / `jobs.db`
  in tasks, commits, or summaries (Principle VI).
- Lines ≤100 chars across all files.
