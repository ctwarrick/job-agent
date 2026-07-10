---
description: "Task list for Overnight Run Scaling (007)"
---

# Tasks: Overnight Run Scaling

**Input**: Design documents from `/specs/007-overnight-scale/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: REQUIRED. This repo enforces TDD (constitution Principle III + CLAUDE.md quality
gates): failing tests are authored and shown red *before* any implementation. Every story
below leads with its test tasks.

**Organization**: Tasks are grouped by user story (US1 P1, US2 P2, US3 P3) so each is an
independently testable increment. The feature is concentrated in `fetch.py`'s dispatch loop
with supporting changes in `store.py`, `main.py`, `digest.py`, and `infra/`.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different file, no dependency on an incomplete task).
- **[Story]**: US1 / US2 / US3, mapping to the spec's user stories.
- Exact file paths are included in each task.

## Path Conventions

Single-project layout: stages in `src/job_agent/`, entrypoint `main.py` at repo root, tests
in `tests/`, infra in `infra/`. Green gates: `uv run pytest` (all changes) plus
`scripts/validate-infra.sh` (when `infra/*` is touched).

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Establish the regression oracle before any change.

- [ ] T001 Confirm a clean baseline: run `uv run pytest` and `scripts/validate-infra.sh` and
  record both green. The `concurrency=1` / unchanged-schedule paths are the A/B oracle every
  later assertion compares against, so a known-green start is mandatory.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared config surface used by all three stories.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [ ] T002 Add the four new config getters to `src/job_agent/fetch.py`, each parsed via the
  existing `resilient._positive_int_env` (fail loud on non-positive — FR-010): `fetch_concurrency()`
  (default 8), `fetch_budget_seconds()` (default 5400), `score_digest_headroom_seconds()`
  (default 1800), and `execution_window_seconds()` (reads `JOBAGENT_EXECUTION_WINDOW_SECONDS`,
  defaulting per contracts/runtime-config.md). Defaults and env names per
  `contracts/runtime-config.md` and `research.md` "Consolidated defaults".

**Checkpoint**: Config getters exist and fail loud — user stories can begin.

---

## Phase 3: User Story 1 - A full overnight run may take hours without being killed (Priority: P1) 🎯 MVP

**Goal**: Raise the execution window to 2 h with a non-overlapping, after-local-midnight,
DST-safe schedule, and fail loud at startup if the fetch budget + headroom can't fit the
window — so the platform never kills a run before the app-level safeguards act.

**Independent Test**: With a budget+headroom that exceeds the window, `main.main()` exits
non-zero naming all three values and nothing is fetched/scored/emailed; and
`scripts/validate-infra.sh` reports the infra compiles with `replicaTimeoutSeconds=7200`,
the DST-safe cron, and the `JOBAGENT_EXECUTION_WINDOW_SECONDS` env pass-through.

### Tests for User Story 1 (write first, prove red)

- [ ] T003 [US1] Failing test in `tests/test_main.py`: when `JOBAGENT_FETCH_BUDGET_SECONDS +
  JOBAGENT_SCORE_DIGEST_HEADROOM_SECONDS > JOBAGENT_EXECUTION_WINDOW_SECONDS`, `main.main()`
  raises `SystemExit` (non-zero) whose message names all three values, and neither fetch,
  score, nor digest runs. Follow the existing fail-loud patterns in `tests/test_main.py`
  (quickstart Scenario 5).

### Implementation for User Story 1

- [ ] T004 [US1] In `main.py`, add the FR-004 startup coherence check *before any external
  effect* (before `store.init()`-driven work): if `fetch_budget + headroom > execution_window`,
  `sys.exit(...)` with the message shape in `contracts/runtime-config.md`. Reads the getters
  from T002.
- [ ] T005 [P] [US1] In `infra/main.bicep`: default `replicaTimeoutSeconds` → 7200, set the
  DST-safe `cronExpression` default, and add the job-container env entry
  `{ name: 'JOBAGENT_EXECUTION_WINDOW_SECONDS', value: string(replicaTimeoutSeconds) }` so the
  app validates against the platform-enforced value (single source of truth).
- [ ] T006 [P] [US1] In `infra/main.bicepparam`: pin the designed end state — `replicaTimeoutSeconds
  = 7200` and `cronExpression = '0 8,10,12 * * *'` (research R2, resolved). This band keeps all
  three attempts after local midnight in both PST and PDT (the `digest_date` invariant),
  accepting the rare PDT third-attempt deadline slip (see R2 Note). Supersedes the live v2.4.2
  stopgap (`0 10,11,12`, 2700s).
- [ ] T007 [US1] Run `scripts/validate-infra.sh` and confirm `OK: infra compiles` with the new
  window, cron, and env pass-through present (quickstart Scenario 6; infra green gate).

**Checkpoint**: US1 is independently deployable — this alone resolves the production incident.

---

## Phase 4: User Story 2 - Fetch never crowds out scoring and the digest (Priority: P2)

**Goal**: A stage-level wall-clock fetch budget that stops cleanly (retaining everything
fetched), dispatches boards least-recently-fully-fetched first (no starvation), and surfaces
budget-deferred boards in the digest as a distinct degraded category.

**Independent Test**: With stubbed slow boards and a tiny injected-clock budget, fetch stops
submitting at the budget, keeps every already-fetched posting, returns the deferred boards,
the pipeline still reaches `RUN_SUCCESS`, and the digest shows an "N boards deferred"
category; a deferred board keeps its prior timestamp and sorts first on the next run.

### Tests for User Story 2 (write first, prove red)

- [ ] T008 [P] [US2] Failing tests in `tests/test_store.py`: the new recency-ordering query
  returns registry sources in `last_converged_at ASC NULLS FIRST` order (never-fetched first),
  and a `greenhouse`/`lever` full fetch advances `last_converged_at` while a stage-deferred
  board does not.
- [ ] T009 [P] [US2] Failing tests in `tests/test_fetch.py` (quickstart Scenario 3): with an
  injected `clock` and a small budget over many stubbed slow boards, `fetch.main()` stops
  submitting new boards past `stage_deadline`, retains everything already fetched, returns the
  deferred boards, and clamps an in-flight board's effective deadline to `min(per_source,
  stage_remaining)`. Assert a deferred board's `last_converged_at` is not advanced (no
  starvation — SC-003).
- [ ] T010 [P] [US2] Failing test in `tests/test_digest.py` (quickstart Scenario 4 report):
  `digest.main()` renders budget-deferred boards as a degraded category distinct from failed
  and per-source-partial, including the deferred count (FR-005).
- [ ] T011 [P] [US2] Failing test in `tests/test_main.py`: a run with deferred boards records
  outcome `degraded` (non-fatal), still sends the digest, and still prints `RUN_SUCCESS`. Add a
  DST assertion: for each attempt start time in the chosen `0 8,10,12` band,
  `store.digest_date("America/Los_Angeles")` returns the intended delivery day (not the prior
  day) in both PST and PDT — enforcing FR-002's after-midnight invariant and FR-011 (the
  missed-deadline marker/query is unaffected by the schedule change).

### Implementation for User Story 2

- [ ] T012 [P] [US2] In `src/job_agent/store.py`, add the recency-ordering query returning
  registry `(source, company)` keys ordered `last_converged_at ASC NULLS FIRST` for the
  dispatch loop to consume.
- [ ] T013 [US2] In `src/job_agent/fetch.py`, dispatch boards in the T012 recency order, and
  call `store.mark_converged` for `greenhouse`/`lever` after a successful whole-board fetch
  (they are always fully fetched in one call — research R4), making convergence total across
  the registry.
- [ ] T014 [US2] In `src/job_agent/fetch.py`, compute `stage_deadline = clock() + fetch_budget_seconds()`
  once at fetch start; stop submitting new boards once `clock() > stage_deadline`; pass each
  dispatched board an effective deadline `min(per_source, stage_remaining)` via `run_source`'s
  existing `clock`/deadline seam; collect never-dispatched boards as deferred
  (`reason="budget_deferred"`) and add them to the returned outcome without breaking the
  `(failed, partial)` shape (data-model + research R5). Depends on T013.
- [ ] T015 [P] [US2] In `src/job_agent/digest.py`, extend `_degradation_facts` and the
  `_render_text`/`_render_html` paths to render the budget-deferred category with its count
  (FR-005), separate from failed and per-source-partial.
- [ ] T016 [US2] In `main.py`, fold deferred boards into the existing `degraded` determination
  and `_degradation_summary` detail so a deferred-only run stays `degraded` (non-fatal) and
  `RUN_SUCCESS` still prints. Depends on T014's return shape.
- [ ] T017 [US2] Confirm `tests/test_resilient.py` stays green unchanged — proof the per-source
  006 semantics are preserved under the new stage-remaining deadline clamp (FR-009; quickstart
  cross-cutting).

**Checkpoint**: US1 + US2 both work — the digest ships even on a pathological night and
reports what was deferred, with no starvation.

---

## Phase 5: User Story 3 - Concurrent fetching keeps the run fast and cheap (Priority: P3)

**Goal**: Fetch boards board-level-concurrently via a bounded `ThreadPoolExecutor` so the
enlarged registry finishes well inside the budget, with results identical to sequential.

**Independent Test**: For the same stubbed inputs, `concurrency=8` stores exactly the same
postings and per-source outcomes as `concurrency=1`, in ≥3x less wall-clock (injected clock);
one hanging/failing board doesn't affect the others.

### Tests for User Story 3 (write first, prove red)

> All three touch `tests/test_fetch.py` (same file) — author them together, not in parallel.

- [ ] T018 [US3] Failing test in `tests/test_fetch.py` (quickstart Scenario 1): stubbed
  latency boards yield identical stored postings and per-source outcomes at
  `JOBAGENT_FETCH_CONCURRENCY=1` and `=8`, with wall-clock at 8 ≥3x faster (assert on an
  injected monotonic-clock delta — SC-004).
- [ ] T019 [US3] Failing test in `tests/test_fetch.py` (quickstart Scenario 2): one stubbed
  board raising/hanging appears in `failed`/`partial` while every other board's postings are
  still stored (failure containment under concurrency — FR-008).
- [ ] T020 [US3] Failing test in `tests/test_fetch.py`: under concurrency, stored postings have
  no loss/dup/corruption versus sequential and per-source log lines stay complete and
  attributable (single-writer lock — FR-008).

### Implementation for User Story 3

- [ ] T021 [US3] In `src/job_agent/fetch.py`, run the dispatch loop through a
  `concurrent.futures.ThreadPoolExecutor(max_workers=fetch_concurrency())`, one board per
  worker; serialize every `store.upsert_postings` under a single module-level
  `threading.Lock` (one SQLite writer); preserve per-source failure containment and
  attributable per-source logging. `concurrency == 1` MUST reproduce today's sequential path
  and ordering exactly, and MUST honor the T014 submission-stop and clamp. (research R1.)

**Checkpoint**: All three stories are independently functional.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [ ] T022 Run the full `uv run pytest` suite green (Python green gate).
- [ ] T023 Run `scripts/validate-infra.sh` green (infra green gate — required because
  `infra/*` changed).
- [ ] T024 [P] Walk quickstart.md Scenarios 1–6 as a final acceptance pass and note any drift;
  check README/config docs for the four new env knobs (drift check per releaser scope).

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (T001)**: no dependencies — establishes the oracle.
- **Foundational (T002)**: depends on Setup — **blocks all user stories** (getters are shared).
- **US1 (T003–T007)**: depends on Foundational. Independently deployable MVP.
- **US2 (T008–T017)**: depends on Foundational. Independent of US1 at runtime.
- **US3 (T018–T021)**: depends on Foundational and, in practice, on **US2** — T021 wraps the
  same `fetch.main()` dispatch loop that T013/T014 establish (recency ordering + budget stop +
  clamp). Build US2 before US3 rather than in parallel.
- **Polish (T022–T024)**: after all desired stories.

### Within each story

- Tests precede implementation and must fail for the right reason first (TDD gate).
- US2: T012 (store ordering) precedes T013 (loop consumes it); T013 precedes T014 (same file,
  budget builds on the reordered loop); T016 (main) depends on T014's return shape.
- US3: T021 depends on the US2 dispatch loop; the three test_fetch tasks share one file.

### Parallel opportunities

- US1: T005 and T006 are different infra files → parallel; T007 validates after both.
- US2 tests: T008 / T009 / T010 / T011 are four different test files → all parallel.
- US2 impl: T012 (store) and T015 (digest) are parallel with each other and with the fetch
  work up to their consumption points; T013/T014/T016 are ordered.
- Across stories: US1 (mostly infra + main.py + test_main.py) can proceed in parallel with
  US2's store/digest tasks if staffed, since they touch different files — but US3 waits on US2.

---

## Parallel Example: User Story 2 tests

```bash
# Author these four failing tests together (different files):
Task: "Ordering + cross-vendor convergence tests in tests/test_store.py"    # T008
Task: "Stage-budget clean-stop + clamp + deferred tests in tests/test_fetch.py"  # T009
Task: "Deferred degraded-category render test in tests/test_digest.py"      # T010
Task: "Deferred-run degraded/RUN_SUCCESS test in tests/test_main.py"        # T011
```

---

## Implementation Strategy

### MVP first (User Story 1 only)

1. T001 Setup (baseline oracle) → T002 Foundational (config getters).
2. T003–T007 US1: startup validation + 2 h window + DST-safe tiled cron + infra validate.
3. **STOP and validate**: this alone stops the platform-kill incident and is deployable.

### Incremental delivery

1. Setup + Foundational → shared config ready.
2. US1 → fail-loud window/schedule → deploy (MVP: platform no longer kills the run).
3. US2 → stage budget + ordering + deferred reporting → the digest always ships.
4. US3 → board-level concurrency → typical night finishes comfortably, cost stays flat.
5. Each story is a green-gated increment; `concurrency=1` remains the regression oracle.

---

## Notes

- [P] = different file, no incomplete dependency. Same-file tasks (the `fetch.py` cluster,
  the `test_fetch.py` cluster) are intentionally *not* marked [P].
- 006 per-source semantics (`tests/test_resilient.py`) must stay green unchanged (FR-009).
- Sensitive files (`profile.md`, `screening_prompt.md`, `registry.toml`, `jobs.db`) are never
  read or quoted by these tasks — stubs provide all inputs.
- Green before done: no task is complete until `uv run pytest` passes; infra tasks also require
  `scripts/validate-infra.sh`. The reviewer re-runs both.
