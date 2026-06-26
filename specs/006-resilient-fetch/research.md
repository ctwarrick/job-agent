# Phase 0 Research: Resilient, Time-Bounded ATS Fetching

All decisions below are grounded in the current code (read during planning); no
NEEDS CLARIFICATION remain.

## R1 — Where does the bounded/resilient/forward-progress logic live?

- **Decision**: A new shared module `src/job_agent/resilient.py` exposing
  `run_source(adapter, source, *, criteria, clock, store_) -> SourceResult`.
  In-scope adapters expose two primitives (`list_postings`, `fetch_description`);
  the orchestration (filter, forward-progress skip, backstop, per-item resilience,
  upsert) lives once in `resilient.run_source`.
- **Rationale**: FR-007 demands one *shared contract* across Workday, iCIMS, and
  Talemetry. Duplicating filter+backstop+forward-progress in three adapters would
  drift; centralizing it is the durable fix and makes the dark adapter correct by
  construction.
- **Alternatives**: (a) per-adapter implementation with shared helpers — more
  duplication, easy drift; (b) push it into `fetch.main` inline — bloats the
  orchestrator and is hard to unit-test in isolation. Rejected.

## R2 — Listing vs. description split per adapter

- **Decision**: Two-phase contract. `list_postings` paginates the **listing only**
  and returns listing-level `Posting` stubs; `fetch_description` does the single
  expensive per-posting retrieval.
  - **Workday**: `list_postings` = the paginated `POST .../jobs` loop (today's
    pagination, minus the per-employer cap); `fetch_description` = the
    `GET .../{externalPath}` detail call. `external_id` = `externalPath`.
  - **Talemetry**: `list_postings` = the job-card pagination (title/location/date
    from cards, `description=""`); `fetch_description` = the detail-page GET +
    selector parse. `external_id` = numeric job id (already so).
  - **iCIMS**: descriptions are **inline** in the listing JSON (no second round-
    trip). `list_postings` returns full postings (with descriptions);
    `fetch_description` is a pass-through returning `posting.description`. The
    orchestration short-circuits the call when a stub already has a description.
    `external_id` = `req_id`.
- **Rationale**: matches each board's real cost shape; gives FR-003/FR-004 (filter
  on listing-level fields, fetch descriptions only for survivors) uniformly, while
  not inventing a fake detail call for iCIMS.
- **Alternatives**: a single `fetch(...)` with an internal filter — cannot express
  "don't fetch the description" without the two-phase split for Workday/Talemetry.

## R3 — The fingerprint-identity constraint (why stubs are never stored)

- **Finding**: `schema.Posting.fingerprint` = `sha256(title|company|location|
  description)[:16]` and is the `postings` PRIMARY KEY; `_migrate_fingerprints`
  re-keyed existing rows to *include* description. Storing a description-less stub
  (`description=""`) and later filling the description would **change the
  fingerprint** → a new row, not an update.
- **Decision**: Never store description-less stubs. A survivor is upserted **only
  after** its description is retrieved, so the fingerprint is final at insert.
  Forward-progress is tracked by the description-independent `external_id` column
  via `store.existing_external_ids(source, company)`. `schema.py` is unchanged.
- **Rationale**: satisfies FR-009 (no re-key, no destructive migration) and keeps
  the deliberate content-based dedupe intact. Rejected postings need not be stored;
  they are re-derived cheaply each run (listing-level, no LLM, no detail call).
- **Alternative considered**: revert the fingerprint to exclude description so stubs
  could be filled in place — rejected: it re-introduces the silent same-title drop
  that `_migrate_fingerprints` fixed, and is a destructive identity change.
- **Accepted tradeoff**: because forward-progress skips already-stored
  `external_id`s, an already-described posting is never re-described, so a later
  description edit on the board is not picked up. Change-detection is out of scope.

## R4 — Forward progress + bounded-staleness alert (FR-015)

- **Decision (progress)**: the store is the progress state. Each run describes only
  survivors whose `external_id` is **not yet stored** for that source, so runs make
  monotonic forward progress and never re-truncate the same prefix; a board of N
  survivors with budget B converges in ⌈N/B⌉ runs.
- **Decision (alert)**: one timestamp per source in a new tiny
  `source_progress(source, company, last_converged_at)` table. Finish-with-
  `remaining==0` → set `last_converged_at=now`. A run that leaves `remaining>0` with
  `now - last_converged_at > JOBAGENT_STALENESS_BOUND_DAYS` → mark the source
  **persistently degraded** (digest-visible). New sources seed `last_converged_at=
  now` for a full grace window.
- **Rationale**: forward progress alone converges only if budget ≥ inflow; the
  timestamp is the minimal honest mechanism to catch a structurally-too-large board
  and surface it loudly (FR-015) instead of drifting silently.
- **Alternatives**: per-run consecutive-truncation counter (needs careful reset
  semantics); pagination cursor table (heavier, and unnecessary once "already
  stored" gives progress for free). Rejected as more complex.

## R5 — Backstop configuration surface (replaces the per-employer cap)

- **Decision**: `JOBAGENT_MAX_DETAIL_PER_SOURCE` (default 150),
  `JOBAGENT_FETCH_DEADLINE_SECONDS` (default 300), `JOBAGENT_STALENESS_BOUND_DAYS`
  (default 7). The backstop fires on whichever bound is hit first. Remove
  `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` from the adapters; keep the registry
  `max_per_employer` key accepted-but-ignored (documented deprecated) so an existing
  git-ignored `registry.toml` does not fail-loud.
- **Rationale**: FR-002 rejects the per-employer cap (drops the long tail). The
  backstop bounds *work per run* while forward progress preserves *full coverage*
  across runs — the two reconcile FR-001 and FR-002. Config-via-env + fail-loud
  matches existing `score.py` guardrail conventions.
- **Alternatives**: a single global wall-clock for the whole fetch stage — coarser,
  can't attribute truncation to a source for FR-014 reporting. Rejected.

## R6 — Clock injection for deterministic deadline tests

- **Decision**: `resilient.run_source` takes an injectable `clock` (default
  `time.monotonic`) and the per-item `time.sleep` stays monkeypatchable as today.
  Tests drive the deadline by feeding a fake monotonic clock; the detail-count cap
  is tested with a small `MAX_DETAIL_PER_SOURCE` and counted stub calls.
- **Rationale**: keeps tests stub-based and no-network (FR-012) while exercising the
  wall-clock backstop deterministically.
