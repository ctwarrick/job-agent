# Feature Specification: LLM Scoring Spend Efficiency

**Feature Branch**: `002-scoring-spend-efficiency`

**Created**: 2026-06-12

**Status**: Draft

**Input**: User description: "Implement Constitution Principle VII for the score stage — make LLM scoring cost proportionate to value via deterministic pre-filtering, prompt caching of the static prefix, a per-run budget cap, and per-run cost observability. Scope is the score stage only."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Filter before you spend (Priority: P1)

The maintainer adds a company to the registry whose board has hundreds of open
roles, most in functions they will never apply to (sales, finance, clinical,
recruiting). Before any posting is sent to the LLM, a deterministic, zero-cost
relevance gate drops the obviously-irrelevant ones, so the model only scores
plausible matches. Rejected postings are recorded with a reason so they are not
re-evaluated on every run and can be explained later.

**Why this priority**: This is the dominant cost lever. The cold-start showed
1,285 postings cost ~$21.5 to score unfiltered; most were irrelevant. Filtering
first delivers the bulk of the savings and is independently valuable even
without the other three capabilities.

**Independent Test**: Seed a set of postings spanning relevant and clearly
irrelevant roles; run scoring; confirm only plausible matches reach the LLM,
irrelevant ones are persisted as rejected-with-reason, and a re-run does not
re-examine them.

**Acceptance Scenarios**:

1. **Given** a batch of postings where some are clearly out-of-scope (wrong
   function, wrong location, below salary floor, too old, wrong seniority),
   **When** the score stage runs, **Then** the out-of-scope postings are never
   sent to the LLM and are recorded with a machine-readable rejection reason.
2. **Given** postings already rejected by the filter on a prior run, **When**
   the score stage runs again, **Then** those postings are not re-evaluated by
   the filter or the LLM (no duplicate work, no duplicate spend).
3. **Given** a posting that passes every deterministic gate, **When** the score
   stage runs, **Then** it is sent to the LLM for nuanced scoring exactly as
   today.
4. **Given** the filter criteria are changed in the runtime tuning file, **When**
   the score stage runs, **Then** the new criteria take effect with no code
   change.

---

### User Story 2 - Per-run budget guardrail (Priority: P2)

The maintainer wants a hard guarantee that a single run can never silently spend
more than an approved amount, even if the filter is mis-tuned or a board returns
an unexpected flood of postings. A configurable cap (maximum postings scored
and/or maximum estimated dollars per run) stops the run loudly rather than
exceeding it; work already done persists so the next scheduled run resumes.

**Why this priority**: The safety net that enforces Constitution Principle I's
"upper bound on LLM calls." Filtering reduces *expected* spend; the cap bounds
*worst-case* spend.

**Independent Test**: Set a low cap; run scoring against a backlog larger than
the cap; confirm the run stops at the cap, persists what it scored, logs the
stop reason, and a subsequent run resumes the remainder.

**Acceptance Scenarios**:

1. **Given** a configured per-run cap and a post-filter backlog larger than the
   cap, **When** the score stage runs, **Then** it scores up to the cap, stops
   loudly with a clear logged reason, and does not exceed the cap.
2. **Given** a run stopped at the cap with partial progress, **When** the next
   run starts, **Then** it resumes scoring the not-yet-scored postings (no
   re-scoring of already-scored ones).
3. **Given** no cap is configured, **When** the score stage runs, **Then** a
   safe default cap applies (the system is never unbounded).

---

### User Story 3 - Cache the static prefix (Priority: P3)

Within a single scoring run, the candidate profile and screening prompt are
identical across every batch. They are cached so they are not re-billed as fresh
input on every call.

**Why this priority**: Reduces the per-batch input cost for postings that do get
scored. Valuable but secondary to not scoring irrelevant postings at all.

**Independent Test**: Run scoring over multiple batches; confirm the static
profile + screening prefix is sent in a cacheable form and that cache reuse is
reflected in the run's reported token usage.

**Acceptance Scenarios**:

1. **Given** a run with more than one batch, **When** batches after the first are
   scored, **Then** the static profile + screening prefix is served from cache
   rather than re-billed as full fresh input.
2. **Given** the profile or screening prompt changes between runs, **When** the
   next run starts, **Then** the new content is used (stale cache is never
   served across a content change).

---

### User Story 4 - Cost observability (Priority: P3)

After each run, the maintainer can see from the logs how many postings were
filtered vs. scored, the token usage, and the estimated dollar cost — without
re-running anything and without waiting for the Anthropic bill.

**Why this priority**: Closes the blindness that made the cold-start cost a
surprise; lets the Anthropic bill be reconciled from logs (Principle I dual-bill
monitoring). Depends on nothing else.

**Independent Test**: Run scoring; confirm a single run-summary log line reports
postings fetched/filtered/scored, input+output tokens, and estimated cost.

**Acceptance Scenarios**:

1. **Given** a completed (or cap-stopped) run, **When** the maintainer reads the
   logs, **Then** a run summary reports counts (filtered, scored, remaining),
   token usage, and estimated cost.
2. **Given** the run stopped at the budget cap, **When** the maintainer reads the
   logs, **Then** the summary makes the cap stop and the remaining backlog
   explicit.

### Edge Cases

- **Empty post-filter set**: every fetched posting is filtered out — the run
  makes zero LLM calls, costs nothing, and reports that clearly (no error).
- **Cap smaller than one batch**: the cap is honored at posting granularity, not
  rounded up to a full batch.
- **Filter criteria file missing or malformed**: per Principle V, fail loud
  before any LLM call rather than silently scoring everything.
- **Posting with missing fields** the filter keys on (no location, no posted
  date): the gate must have a defined default (pass to LLM vs. reject) so it is
  never an unhandled crash. [NEEDS CLARIFICATION: should the filter fail-open
  (send to LLM) or fail-closed (reject) on missing filterable fields?]
- **Estimated cost drift**: pricing inputs change over time; estimated cost is a
  configurable approximation, not a billing source of truth.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The score stage MUST apply a deterministic, zero-cost relevance
  filter to each candidate posting BEFORE any LLM call, sending only plausible
  matches to the model.
- **FR-002**: The relevance filter criteria MUST be defined in runtime tuning
  files (not hardcoded), so they can be changed without a code change
  (Constitution Principle IV).
- **FR-003**: Postings rejected by the filter MUST be persisted with a
  machine-readable rejection reason and MUST NOT be re-evaluated on subsequent
  runs.
- **FR-004**: The concrete filter criteria MUST cover, at minimum:
  [NEEDS CLARIFICATION: which buckets/title keywords, which locations
  (remote? specific metros?), which seniority bands, what maximum posting age,
  and the salary-floor handling count a posting as "plausible"? — to be resolved
  in /speckit-clarify].
- **FR-005**: The score stage MUST enforce a configurable per-run budget cap,
  expressed as a maximum number of postings scored and/or a maximum estimated
  dollar spend per run. [NEEDS CLARIFICATION: cap by posting count, by estimated
  dollars, or both — and what default values?]
- **FR-006**: When the cap is reached, the run MUST stop scoring further
  postings, surface the stop loudly in logs (Constitution Principle V), and MUST
  NOT exceed the cap.
- **FR-007**: Partially-scored progress MUST persist so a subsequent run resumes
  the not-yet-scored postings without re-scoring completed ones (the existing
  incremental-commit behavior is preserved).
- **FR-008**: A safe default cap MUST apply when none is configured; the system
  MUST never run unbounded.
- **FR-009**: The static portion of each scoring request that is identical across
  batches in a run (candidate profile + screening prompt) MUST be sent in a
  cacheable form so it is not re-billed as fresh input on every batch.
- **FR-010**: A change to the profile or screening prompt MUST be reflected on
  the next run (no stale cached content served across a content change).
- **FR-011**: Each run MUST emit a run-summary log reporting postings fetched,
  filtered (with reason breakdown), and scored; remaining backlog; token usage;
  and estimated dollar cost.
- **FR-012**: Cap thresholds and pricing inputs used for cost estimation MUST be
  environment/runtime-configurable, consistent with existing config-via-env-var
  conventions.
- **FR-013**: All new behavior MUST be confined to the score stage; fetch and
  digest behavior MUST be unchanged.
- **FR-014**: The filter and cap MUST fail loud (exit non-zero, no partial
  external effect) when their configuration is missing or invalid, before any
  LLM call.

### Key Entities *(include if feature involves data)*

- **Posting (scoring state)**: existing entity; gains an explicit lifecycle of
  *unscored → filtered-out (with reason) | scored*, so each posting is processed
  at most once and its disposition is explainable.
- **Filter criteria**: runtime-file-defined set of deterministic gates (function/
  bucket, title keywords, location, seniority, posting age, salary floor) that
  decide plausible vs. rejected. Subjective tuning, not code.
- **Run budget**: configurable per-run cap (posting count and/or estimated
  dollars) plus the pricing inputs used to estimate cost.
- **Run summary**: per-run record of counts (fetched/filtered/scored/remaining),
  token usage, and estimated cost, emitted to logs.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For a representative large board, the number of postings sent to
  the LLM on cold-start is reduced by at least 70% versus scoring everything
  fetched (filter effectiveness).
- **SC-002**: No single run's spend exceeds the configured cap, verified by the
  run-summary estimated cost staying at or under the cap in every run.
- **SC-003**: Steady-state daily runs (only genuinely new postings) cost on the
  order of cents, not dollars.
- **SC-004**: Every posting not sent to the LLM has a recorded rejection reason;
  zero plausible matches are silently dropped without a reason.
- **SC-005**: A maintainer can determine a run's postings-filtered, postings-
  scored, token usage, and estimated cost from the logs alone, without re-running.
- **SC-006**: Re-scoring rate is zero: no posting already scored or already
  filtered is re-sent to the LLM on a later run.

## Assumptions

- The score stage already commits scored rows incrementally per batch (verified:
  366 rows survived a mid-run kill), so "resume" requires only that unscored work
  be identifiable — no new transactional design is needed.
- Scoring remains batch-based against a single configurable model
  (`JOBAGENT_MODEL`); this feature does not introduce multi-model tiering
  (a cheaper coarse-pass model is out of scope for v1, though not precluded
  later).
- Filtering operates on already-fetched posting fields (title, location,
  description, posted date, etc.); it does not change what fetch retrieves.
- Estimated cost is an approximation from configurable per-token prices; it is
  for observability and the cap, not an authoritative bill.
- Cap-stop behavior is a clean, logged stop that preserves progress; whether the
  process exits non-zero on a cap stop is a tuning detail to settle in planning,
  defaulting to a loud-but-non-failing stop so the scheduled job is not marked
  failed for a normal cap event.
- Filter tuning and cap thresholds follow the existing pattern: runtime files for
  subjective criteria, env vars for operational limits.
