# Feature Specification: Overnight Run Scaling

**Feature Branch**: `007-overnight-scale`

**Created**: 2026-07-09

**Status**: Draft

**Input**: User description: "Scale the overnight pipeline to the enlarged registry so the
digest reliably ships by the morning deadline. Three coordinated changes: (1) raise the
job's execution time limit so a full fetch+score+digest run may take multiple hours
overnight, adjusting the scheduled attempts so retries don't overlap; (2) parallelize the
fetch stage across boards (network-bound) to cut wall-clock time; (3) add a per-run fetch
time budget (clean-stop deadline like the existing max-postings/max-cost caps) so fetch
always leaves enough headroom for score and digest to complete within the run window, with
later scheduled attempts picking up remaining boards."

## Context

On 2026-07-08 the registry roughly tripled in size (large boards included). All three of
the next morning's scheduled attempts were force-killed by the platform at the 15-minute
execution limit while still fetching, so no digest was sent and the missed-deadline alert
fired. The per-source backstops from `006-resilient-fetch` worked as designed, but nothing
bounds the fetch **stage** as a whole: with enough registered boards, the sum of
individually-bounded sources exceeds the whole run's execution window. This feature makes
run capacity scale with the registry: a bigger execution window (US1), a stage-level fetch
budget so scoring and digest always run (US2), and concurrent fetching so the window is
used efficiently (US3).

## Clarifications

### Session 2026-07-09

- Q: How large should each attempt's execution window be (it determines attempt
  spacing, start times, and interacts with digest-date computation at run
  start)? → A: 2 hours — attempts start ~00:00/02:00/04:00 local, all after
  local midnight so the existing digest-date semantics are untouched, and the
  final window closes at the 06:00 local deadline.
- Q: What registry scale should the design target (concurrency + fetch budget
  sizing)? → A: ~50 boards. Today's wired registry is 35 boards (6 Greenhouse,
  3 Lever, 26 Workday); the 302-row company list is a candidate pool, not the
  wired set. Design for ~50 Workday-heavy boards — the bottleneck is the slow
  per-posting-detail Workday vendor, not raw board count — with no need to scale
  to hundreds.
- Q: How are budget-deferred boards prioritized in later runs to guarantee no
  starvation? → A: Least-recently-fully-fetched first. Boards are ordered by
  their last full-fetch timestamp (reusing the `006-resilient-fetch`
  `converged_at`/last-success state); a board truncated by the stage budget
  keeps its prior timestamp, so it sorts ahead on the next run and the oldest
  board always rises to the top.
- Q: What is the default fetch-stage budget and the reserved headroom for score
  + digest within the 2-hour window? → A: 90-minute fetch budget, 30 minutes
  reserved for scoring + digest. Startup validation (FR-004) fails loud if
  configured fetch budget + reserved headroom would exceed the execution window.
  Both values are configuration with these defaults.
- Q: At what granularity is fetch parallelized? → A: Board-level only. N boards'
  fetch loops run concurrently; each board's internal per-posting detail-fetch
  loop stays sequential, so the `006-resilient-fetch` per-source deadline / cap /
  forward-progress backstops are untouched. Within-board detail-fetch
  parallelism is explicitly out of scope for this feature.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A full overnight run may take hours without being killed (Priority: P1)

The maintainer has grown the registry to several times its original size. The overnight
run now legitimately needs more than 15 minutes end-to-end. The job's execution window is
raised so a complete fetch → score → digest pass may take multiple hours, the day's
scheduled attempts are spaced so a retry never fires while the previous attempt can still
be running, and the final attempt still finishes before the 06:00 local delivery deadline.

**Why this priority**: This is the production incident. Every attempt on 2026-07-09 was
killed mid-fetch by the platform's execution limit; nothing else in this feature matters
if the platform kills the run before the app-level safeguards can act.

**Independent Test**: Deploy the revised schedule/window configuration and confirm (a) a
run exceeding 15 minutes is not killed, (b) consecutive attempts never run concurrently,
and (c) the last attempt's window closes before the delivery deadline.

**Acceptance Scenarios**:

1. **Given** the enlarged registry, **When** the first scheduled attempt runs for longer
   than the old 15-minute limit, **Then** the platform does not terminate it and the run
   proceeds to scoring and digest.
2. **Given** a first attempt still in progress, **When** the next scheduled attempt would
   fire, **Then** attempts are spaced such that the retry starts only after the prior
   attempt's window has closed (no overlapping executions, no wasted retry).
3. **Given** the final scheduled attempt of the day, **When** it runs for its full
   permitted duration, **Then** it still completes before the 06:00 local delivery
   deadline, and the missed-deadline alert semantics are unchanged.

---

### User Story 2 - Fetch never crowds out scoring and the digest (Priority: P2)

No matter how large the registry grows or how slowly boards respond, the fetch stage stops
cleanly at a configurable time budget — like the existing max-postings and max-cost caps —
and hands over to scoring and digest with guaranteed headroom. Boards not reached within
the budget are picked up by later attempts or the next day's run, and no board is starved
indefinitely.

**Why this priority**: This is the reliability guarantee. US1 buys a bigger window, but
only a stage-level budget guarantees the digest ships even on a pathological night. It
converts "platform kills the run, nothing is delivered" into "run delivers a digest and
visibly reports what was deferred."

**Independent Test**: With stubbed slow boards (no network), set a small fetch budget and
run the pipeline; confirm fetch stops cleanly at the budget, scoring and digest complete,
the run-success marker is emitted, and the digest reports the deferred boards.

**Acceptance Scenarios**:

1. **Given** a registry whose full fetch would exceed the fetch-stage budget, **When** the
   run executes, **Then** fetch stops cleanly at the budget, every already-fetched posting
   is retained, and scoring and digest complete within the run's window.
2. **Given** a run whose fetch stage was budget-truncated, **When** the digest is sent,
   **Then** the digest visibly reports the fetch stage as degraded, including how many
   boards were deferred.
3. **Given** boards deferred by a previous run, **When** the next run executes, **Then**
   deferred boards are prioritized ahead of recently-fetched ones, so that every
   registered board reaches a full fetch within a bounded number of runs (no starvation).
4. **Given** a budget expiry mid-board, **When** fetch stops, **Then** the partial-source
   forward-progress guarantees from `006-resilient-fetch` still hold for that board.

---

### User Story 3 - Concurrent fetching keeps the run fast and cheap (Priority: P3)

Fetching is network-bound waiting: boards spend most of their time blocked on remote ATS
responses. The fetch stage processes multiple boards concurrently (bounded concurrency) so
the enlarged registry completes well inside the fetch budget on a normal night, the run
finishes hours before the deadline, and compute time — billed by the second — stays
proportionate.

**Why this priority**: Performance and cost efficiency. US1+US2 already guarantee
delivery; concurrency makes the typical night comfortable (full coverage, no deferrals)
and keeps the longer execution window from inflating the compute bill.

**Independent Test**: With stubbed boards of known simulated latency (no network),
confirm the fetch stage's wall-clock time with concurrency N is a fraction of the
sequential time, and that results (postings stored, per-source outcomes, failure
containment) are identical to a sequential run.

**Acceptance Scenarios**:

1. **Given** a registry of many boards with realistic response latency, **When** fetch
   runs with bounded concurrency, **Then** stage wall-clock time drops materially versus
   sequential fetching (see SC-004) with identical stored results.
2. **Given** one board failing or hanging while others are in flight, **When** fetch
   runs concurrently, **Then** per-source failure containment behaves exactly as it does
   sequentially: the bad board is reported failed/partial and no other board is affected.
3. **Given** concurrent fetching, **When** postings from multiple boards are stored,
   **Then** no postings are lost, duplicated, or corrupted, and per-source log lines
   remain attributable to their board.

---

### Edge Cases

- **Overlap despite spacing** (clock drift, a manual off-schedule run): the existing
  in-flight startup check already makes the second execution a no-op; spacing exists so a
  scheduled retry is not wasted on a no-op, not as the only overlap defense.
- **Budget expires before any board completes** (e.g. misconfigured tiny budget): fetch
  reports all boards deferred, scoring proceeds on whatever is already stored, and the
  digest reports the degradation loudly.
- **Budget larger than the execution window** (misconfiguration): the run must fail loud
  at startup rather than let the platform kill it mid-run; configuration coherence is
  validated before any external effect.
- **Registry keeps growing faster than one night's budget**: full coverage is reached
  across successive runs via deferred-board prioritization; if a board cannot reach a
  full fetch within the configured staleness bound, it is surfaced as persistent
  degradation (same loud signal as `006-resilient-fetch`).
- **All boards slow simultaneously** (regional network issue): stage budget fires, digest
  ships from what was fetched, deferred boards surface in the digest.
- **Platform kill as last resort**: the execution window is sized so app-level budgets
  always fire first; a platform kill becomes a signal of a defect, not a routine event.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The job's execution window MUST permit a complete fetch → score → digest
  run of up to 2 hours, and the window size MUST be deployment configuration, not code.
- **FR-002**: The day's three scheduled attempts MUST start after local midnight, spaced
  2 hours apart (~00:00 / 02:00 / 04:00 local) so no attempt fires while a prior attempt's
  execution window is still open, and the final attempt's window MUST close at or before
  the 06:00 local delivery deadline. Starting all attempts after local midnight preserves
  the existing run-start digest-date computation unchanged. The number of daily attempts
  (three) and the deadline (06:00 local) are unchanged.
- **FR-003**: The fetch stage MUST enforce a configurable stage-level wall-clock budget
  (default 90 minutes). On expiry it MUST stop cleanly — retaining everything fetched so
  far — and the run MUST proceed to scoring and digest. This budget is a peer of the
  existing per-run max-postings and max-cost caps and follows the same configuration
  conventions.
- **FR-004**: The fetch budget plus a configurable reserved headroom for scoring and
  digest (default 30 minutes) MUST be validated against the execution window at startup:
  a configuration in which fetch budget + reserved headroom exceeds the window MUST fail
  loud before any external effect.
- **FR-005**: A budget-truncated fetch MUST be surfaced in the digest as degradation,
  distinct from healthy, failed, and per-source-partial states, including the count of
  deferred boards — consistent with how `006-resilient-fetch` surfaces partial sources.
- **FR-006**: The fetch stage MUST process boards in least-recently-fully-fetched order
  (by each board's last full-fetch timestamp, reusing the `006-resilient-fetch`
  forward-progress state). A board truncated by the stage budget MUST retain its prior
  timestamp so it sorts ahead on the next run. This ordering guarantees every registered
  board reaches a full fetch within a bounded, configurable number of runs; a board
  exceeding the staleness bound MUST surface as persistent degradation.
- **FR-007**: The fetch stage MUST process multiple boards concurrently with a bounded,
  configurable concurrency level; a concurrency level of one MUST reproduce today's
  sequential behavior. Concurrency is board-level only: each board's internal
  per-posting detail-fetch loop remains sequential, and the `006-resilient-fetch`
  per-source backstops are unchanged. Within-board detail-fetch parallelism is out of
  scope.
- **FR-008**: Concurrent fetching MUST preserve the existing guarantees unchanged:
  per-source failure containment, per-source partial/truncation reporting, posting
  de-duplication and storage integrity, and per-source attributable log output.
- **FR-009**: The stage budget MUST compose with the per-source backstops from
  `006-resilient-fetch` (per-source detail cap, per-source deadline, staleness bound):
  per-source limits bound each board, the stage budget bounds their sum, and whichever
  fires first stops the corresponding scope cleanly.
- **FR-010**: All new knobs (execution window, attempt schedule, stage budget,
  concurrency) MUST be configurable without code changes, MUST have production-safe
  defaults, and MUST fail loud on invalid values, per existing configuration conventions.
- **FR-011**: The missed-deadline alert contract (the run-success marker and its query)
  MUST continue to function unchanged under the new schedule and window.

### Key Entities

- **Fetch-stage budget**: a per-run wall-clock allowance for the whole fetch stage;
  distinct from the per-source deadline of `006-resilient-fetch`, which bounds one board.
- **Board fetch state**: per-board record of fetch recency/completeness that orders
  deferred boards ahead of recently-completed ones and detects starvation; extends the
  forward-progress state introduced by `006-resilient-fetch`.
- **Attempt schedule**: the day's scheduled run start times plus the shared execution
  window; together they must tile the night without overlap and end before the deadline.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With the current enlarged registry (~3x the size that fit the old window),
  the overnight pipeline delivers the digest by the 06:00 local deadline, and the
  missed-deadline alert does not fire on a healthy night.
- **SC-002**: A run whose fetch workload would exceed its budget still delivers a digest:
  scoring and digest complete, the run-success marker is emitted, and deferred boards are
  visible in the digest — zero platform-killed executions under normal operation.
- **SC-003**: Every registered board reaches a full fetch at least once within the
  configured staleness bound (default: a week), regardless of registry size — no board is
  silently starved.
- **SC-004**: With concurrency enabled, fetch-stage wall-clock time for the enlarged
  registry drops by at least 3x versus sequential fetching under equivalent conditions.
- **SC-005**: Total cloud spend stays within the constitutional $50/month all-in ceiling;
  the longer window does not materially raise compute cost because billing follows actual
  run duration, which concurrency keeps proportionate.

## Assumptions

- The three-attempts-per-night retry model and the 06:00 America/Los_Angeles delivery
  deadline are retained; only the attempts' start times and shared window change.
- Today's wired registry is 35 boards (6 Greenhouse, 3 Lever, 26 Workday); the 302-row
  `Company List.csv` imported on 2026-07-08 is a candidate pool, not the wired set (only
  out-of-the-box adapter vendors were wired). Design for ~50 boards of headroom, not
  hundreds. The dominant cost is the 26 Workday boards' per-posting detail fetching, not
  raw board count.
- Fetching is dominated by network waiting, so bounded concurrency yields near-linear
  wall-clock reduction without meaningful compute cost increase.
- Compute is billed by actual execution time (consumption-based, scale-to-zero per the
  constitution), so a longer *permitted* window costs nothing on nights when the run
  finishes early.
- Remote ATS boards tolerate a modest concurrency level from a single client; per-source
  request pacing stays within the bounds already established by the adapters.
- The existing in-flight startup check remains the correctness defense against
  overlapping executions; schedule spacing is an efficiency measure on top of it.
- `006-resilient-fetch` per-source backstops (detail cap, per-source deadline, staleness
  bound) remain in force and unchanged in semantics.
