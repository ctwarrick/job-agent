# Feature Specification: Resilient, Time-Bounded ATS Fetching

**Feature Branch**: `006-resilient-fetch`

**Created**: 2026-06-26

**Status**: Draft

**Input**: User description: "Resilient, time-bounded ATS fetching (cross-adapter)" — the
daily pipeline stopped delivering the digest after a large-board ATS adapter went live,
because that adapter's per-posting detail fetching makes a single source run long enough to
exhaust the job's execution window, and a single per-item error discards the whole source.
The fix must be bounded, full-coverage (no per-employer cap), and applied across every
adapter that shares the pattern.

## Clarifications

### Session 2026-06-26

- Q: How is FR-001's "bounded regardless of board size" actually guaranteed —
  lazy/filtered description retrieval alone, an explicit deadline, or both? → A:
  Both — lazy/filtered description retrieval is the primary mechanism, plus a
  configurable fetch-stage safety backstop (per-source detail-retrieval cap
  and/or wall-clock deadline) that stops loudly and reports degradation rather
  than letting a pathological board exhaust the run's execution window.
- Q: For a partly-fetched source (per-item skips or a backstop cutoff), where is
  the degradation surfaced — digest or logs only? → A: In the digest itself,
  distinct from healthy and wholly-failed sources, with enough detail (skipped
  count, whether the backstop fired) to judge the loss without reading logs.
- Q: How does the backstop avoid a permanent backlog so I'm never persistently
  behind reality? → A: Bounded staleness — each run makes forward progress on the
  truncated remainder (never re-truncating the same prefix) so a source reaches
  full coverage within a configurable run/day bound, and a source that exceeds the
  bound is surfaced as a loud, persistent degradation rather than silent drift.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Digest still arrives when a board is very large (Priority: P1)

The maintainer adds a company whose board carries hundreds-to-thousands of open
requisitions. The overnight run must still fetch that board, score the relevant postings,
and deliver the morning digest within the scheduled execution window — instead of the run
being force-killed mid-fetch so no digest is ever sent and only repeated failure alerts
arrive.

**Why this priority**: This is the production incident. Since a large-board adapter went
live, no run has reached the scoring or digest stage; the maintainer receives failure
alerts and no digest. Restoring reliable daily delivery is the entire point of the project
and the minimum viable outcome of this feature.

**Independent Test**: Configure a registry that includes one board reporting ~900 open
postings (stubbed, no network). Run the full pipeline and confirm it reaches the
run-success marker and sends a digest within the execution-window budget, without the fetch
stage exhausting that budget.

**Acceptance Scenarios**:

1. **Given** a registry with a board of ~900 open postings, **When** the daily run
   executes, **Then** the fetch stage completes with enough headroom for scoring and digest
   to finish inside the execution window, and the run-success marker is emitted.
2. **Given** a large board where the deterministic filter rejects most postings on
   listing-level fields, **When** fetch runs, **Then** the number of expensive per-posting
   detail retrievals is proportional to the postings that pass the filter, not to the total
   board size.
3. **Given** the same large board, **When** the run completes, **Then** the digest is
   delivered (or the no-new-matches notice is sent) rather than the run being killed before
   any digest path executes.

---

### User Story 2 - One bad posting does not zero out a whole board (Priority: P2)

When a single posting's detail page (or a single listing page) fails transiently — a 502, a
read timeout, a non-JSON response — that one item is skipped and logged, and the rest of the
board is still ingested. A transient blip on item 850 of 900 must not throw away the 849
postings already collected.

**Why this priority**: Per-item resilience both protects against wasted work (minutes of
fetching discarded by one error) and reinforces graceful degradation. It is independently
valuable even without Story 1, and is independently testable.

**Independent Test**: Stub a source whose Nth detail/page request raises while the others
succeed. Confirm the source returns all the other postings, the failure is logged, and the
run continues.

**Acceptance Scenarios**:

1. **Given** a board where one posting's detail retrieval fails, **When** the source is
   fetched, **Then** that posting is skipped and logged and every other posting on the board
   is still ingested.
2. **Given** a board where one listing page fails mid-pagination, **When** the source is
   fetched, **Then** the postings already collected from earlier pages are retained and the
   source continues rather than discarding them.
3. **Given** an entire source that fails (e.g. every request errors), **When** the run
   executes, **Then** the source is reported as a failed source, the digest is still
   delivered from the sources that succeeded, and the degradation is surfaced — preserving
   the existing per-source containment behavior.

---

### User Story 3 - Every affected adapter is bounded, including the dark one (Priority: P3)

The same boundedness and per-item resilience apply to every adapter that performs
per-posting detail retrieval or unbounded full-board pagination — not just the one that
triggered the incident. The currently inactive (dark) adapter that shares the exact pattern
is fixed now, so the same outage cannot recur when it is eventually enabled.

**Why this priority**: The defect is a class, not a single adapter. Fixing only the live
adapter leaves a known landmine that reproduces the outage the day the dark adapter is
turned on. Generalizing the contract is what makes the fix durable.

**Independent Test**: Run the adapter test suites for each affected adapter (the live
large-board adapter, the milder paginating adapter, and the dark adapter) and confirm each
demonstrates bounded request volume and per-item resilience under stubs.

**Acceptance Scenarios**:

1. **Given** the set of adapters that do per-posting detail retrieval or unbounded
   pagination, **When** their behavior is tested, **Then** each one fetches expensive detail
   only for filter-surviving postings and skips-not-aborts on a per-item failure.
2. **Given** the dark/inactive adapter, **When** its tests run, **Then** it exhibits the
   same bounded, resilient behavior even though no live source is wired to it.
3. **Given** the two single-request, inline-description adapters that do not share the
   pattern, **When** this feature is delivered, **Then** their behavior is unchanged (out of
   scope).

---

### User Story 4 - Accurate "new postings" count in logs (Priority: P4)

The per-source fetch log line reports the true number of newly stored postings, so the logs
used to diagnose an incident are trustworthy rather than reporting double the real count.

**Why this priority**: Small, isolated correctness fix. It does not restore delivery, but
the inflated count actively misled diagnosis of this very incident, so it is worth
correcting while the fetch path is open.

**Independent Test**: Upsert a known set of brand-new postings and confirm the reported
"new" count equals the number of postings inserted, not twice that.

**Acceptance Scenarios**:

1. **Given** a fetch that inserts K brand-new postings, **When** the source's log line is
   emitted, **Then** it reports K new, not 2K.
2. **Given** a fetch where every posting already exists, **When** the source is fetched,
   **Then** it reports 0 new.

---

### Edge Cases

- **Large board, few survivors**: thousands of open reqs but only a handful pass the
  filter — fetch must stay fast because only survivors incur the expensive detail
  retrieval.
- **Survivor's description cannot be retrieved**: a posting passes the filter but its
  description retrieval fails — it is left unscored and retryable on a later run, never
  scored on empty text and never silently dropped.
- **Survivor's description comes back empty**: excluded from scoring (the scorer is never
  asked to judge an empty description), consistent with existing handling.
- **Re-fetching an already-ingested board**: produces no duplicate rows and an accurate
  (zero, or only-the-truly-new) new count.
- **Whole listing page fails**: that page is skipped and logged, and the source is flagged
  partial / degraded in the digest (FR-014); if every page fails the source is reported
  failed (existing per-source containment) without affecting other sources.
- **Recovery without data loss**: the accumulated backlog of postings ingested-but-never-
  scored during the outage becomes scorable automatically on the first successful run, with
  no database wipe and no loss of application-status or digest-sent history.
- **Pathological board exceeds the fetch budget**: a board whose surviving-detail volume or
  listing pagination would otherwise blow the window hits the configurable fetch-stage
  backstop (FR-013) — fetching that source stops at the budget, the source is reported
  degraded / partially fetched, and the run still reaches scoring and digest rather than
  being killed mid-fetch. Subsequent runs make forward progress on the remainder so coverage
  converges within the configured staleness bound (FR-015); a board that never converges is
  surfaced as a persistent degradation rather than silently staying behind.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The fetch stage MUST be bounded such that no single source can consume the
  entire run's execution window; fetch MUST complete with enough margin for scoring and
  digest to finish within the platform's execution-window limit, regardless of how many
  open requisitions a board carries. Boundedness is guaranteed by two mechanisms working
  together: lazy/filtered description retrieval as the primary mechanism (FR-003), and an
  explicit fetch-stage safety backstop (FR-013) as a fail-loud guardrail for pathological
  boards where the primary mechanism's assumptions do not hold.
- **FR-002**: The system MUST achieve full-board coverage WITHOUT a per-employer posting
  cap — it MUST be able to represent every open, in-scope posting on a board regardless of
  board size. A per-employer cap is explicitly rejected as the mechanism, because it drops
  the long tail of large boards and defeats the project's breadth-of-coverage purpose.
- **FR-003**: The system MUST NOT retrieve a posting's expensive full description for
  postings the deterministic pre-LLM filter would reject. Descriptions MUST be obtained only
  for postings that pass the filter and are about to be scored (consistent with Principle
  VII, "filter before you spend").
- **FR-004**: The deterministic filter MUST be able to decide a posting on listing-level
  fields (title, location, posting date) that are available without a per-posting detail
  retrieval.
- **FR-005**: A failure retrieving one posting's detail — or one listing page — MUST be
  logged and skipped, and the source MUST continue with its remaining postings/pages. It
  MUST NOT discard postings already collected for that source.
- **FR-006**: A per-source failure MUST remain non-fatal to the run: the digest is delivered
  from the sources that succeeded and the degradation is reported visibly (preserving the
  existing per-source containment and degraded-run reporting).
- **FR-014**: Partial-source degradation MUST be surfaced in the digest itself, not in logs
  alone: a source that was only partly fetched — because individual postings or pages were
  skipped (FR-005) or because the fetch-stage backstop truncated it (FR-013) — MUST be
  reported in the digest as degraded / partial, distinct from both a healthy source and a
  wholly-failed source (FR-006), with enough detail (e.g. count of skipped items and whether
  the backstop fired) for the maintainer to judge the loss without reading logs.
- **FR-007**: The bounded-and-resilient behavior MUST be applied as a shared contract to
  EVERY adapter that performs per-posting detail retrieval or unbounded full-board
  pagination — currently the live large-board adapter (Workday), the milder paginating
  adapter (iCIMS), and the inactive/dark adapter (Talemetry). The dark adapter MUST receive
  the fix even though no live source is wired to it.
- **FR-008**: Adapters that fetch a whole board in a single request with descriptions inline
  (Greenhouse, Lever) are out of scope and MUST remain unchanged.
- **FR-009**: Recovery from the outage MUST NOT require deleting the database or re-ingesting
  postings. Postings already ingested but unscored MUST become scorable on the next
  successful run with no manual intervention, and application-status and digest-sent history
  MUST be preserved.
- **FR-010**: A posting that passes the filter but whose description cannot be retrieved
  MUST be left unscored (retryable on a later run) — it MUST NOT be scored on empty text and
  MUST NOT be silently dropped from the store.
- **FR-011**: The per-source "new postings" count reported in the fetch logs MUST equal the
  actual number of newly inserted postings (it MUST NOT be inflated by the companion
  application-record insert).
- **FR-012**: The change MUST follow the project's test-first workflow: failing tests for
  the bounded/resilient behavior are authored and observed red before implementation, and
  all adapter tests remain stub-based with no network calls.
- **FR-013**: The fetch stage MUST enforce an explicit, configurable safety backstop — a
  per-source cap on expensive detail retrievals and/or a wall-clock deadline — that, when
  reached, stops fetching that source loudly (Principle V) and reports the source as
  partially fetched / degraded rather than allowing it to exhaust the run's execution
  window. The backstop is a guardrail expected NOT to trigger under the stated filter
  assumptions: in the normal case it never fires and full-board coverage (FR-002) is
  achieved. It reconciles with FR-002 because, when it does fire, it surfaces visible
  degradation instead of silently capping coverage, and the run still proceeds to scoring
  and digest. The cap/deadline values MUST be environment-configurable, consistent with
  the project's config-via-env-var and fail-loud conventions. When the backstop truncates a
  source, the truncation MUST be transient rather than a steady state: forward progress and
  eventual full coverage across runs are required (FR-015).
- **FR-015**: The backstop MUST NOT create a permanent backlog. When a source is truncated by
  the backstop (FR-013), each subsequent run MUST make forward progress on the un-fetched /
  un-described remainder — it MUST NOT re-fetch and re-truncate the same prefix every run — so
  the source reaches full in-scope coverage (FR-002) within a configurable bound (a maximum
  number of runs / days). If a source remains truncated beyond that configured staleness
  bound, the run MUST surface it as a loud, persistent degradation (Principle V), so
  "permanently behind reality" becomes a reported failure rather than silent drift. The
  forward-progress mechanism (e.g. resumable pagination cursor, oldest-first draining of the
  unscored backlog, or rotation) is a plan-phase design decision; the draining reuses the
  existing unscored-backlog behavior (FR-009, FR-010).

### Key Entities *(include if feature involves data)*

- **Posting (listing vs. description)**: a job posting has cheap listing-level attributes
  (title, location, posting date, canonical URL, identity) that are available without a
  per-posting detail call, and an expensive description that requires (in some adapters) a
  separate retrieval. The description is needed only for postings that survive the filter
  and reach scoring.
- **Source / Adapter**: a per-vendor fetcher. For this feature, adapters are classified by
  cost shape: those that require per-posting detail retrieval or unbounded pagination
  (in-scope: Workday, iCIMS, Talemetry) versus single-request inline-description adapters
  (out of scope: Greenhouse, Lever).
- **Run execution window**: the fixed, platform-imposed time budget for one scheduled
  execution, with no automatic retry within a single execution. The fetch stage must fit
  inside it with margin for scoring and digest.
- **Unscored backlog**: postings already stored but not yet scored or filter-rejected; these
  are inherently eligible for scoring and drain automatically once a run completes fetch. The
  same drain-across-runs behavior covers any remainder the backstop deferred (FR-013, FR-015)
  — a truncated board catches up over subsequent runs rather than staying permanently behind.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A daily run that includes at least one board with ~900+ open postings reaches
  the run-success marker and sends a digest (or the no-new-matches notice) within the
  execution window on 100% of days — no run is killed mid-fetch by the window limit.
- **SC-002**: Full-board coverage is achieved with no per-employer cap configured: 100% of a
  board's open, in-scope postings are represented in the store after a successful run.
- **SC-003**: A single transient detail/page failure reduces the affected source's ingested
  postings by at most the one failed item; 0% of already-collected postings for that source
  are lost to one transient error.
- **SC-004**: For a large board, the number of expensive per-posting description retrievals
  performed in a run scales with the count of filter-surviving postings, not with total
  board size.
- **SC-005**: Recovery from the outage requires no database deletion and no re-ingestion;
  the pre-existing unscored backlog is fully drained by automatic subsequent runs.
- **SC-006**: Every in-scope adapter (Workday, iCIMS, Talemetry) demonstrates bounded
  request volume and per-item resilience under automated stub tests, including the dark
  Talemetry adapter; the out-of-scope adapters (Greenhouse, Lever) are unchanged.
- **SC-007**: The "new postings" count in the fetch logs equals the actual number of rows
  inserted (no 2× inflation).
- **SC-008**: No run is killed by the platform execution-window limit even for a
  pathological board: when a source's fetch reaches its configured backstop (detail-
  retrieval cap or wall-clock deadline), fetching that source stops at the budget, the
  source is reported degraded / partially fetched, and the run still reaches the digest on
  100% of such runs.
- **SC-009**: A run in which a source was only partially fetched (items or pages skipped, or
  the backstop fired) produces a digest that visibly flags that source as degraded / partial
  on 100% of such runs — the degradation is surfaced in the digest, not in logs alone.
- **SC-010**: A chronically oversized board converges: starting from a backstop-truncated
  state, repeated daily runs reach full in-scope coverage within the configured staleness
  bound — no posting's coverage lag grows without limit — and a source that exceeds the bound
  is flagged as a persistent degradation rather than drifting silently.

## Assumptions

- The listing endpoint of each in-scope adapter returns the filter's required fields (title,
  location, posting date) without a per-posting detail call — confirmed by current adapter
  behavior for the large-board adapter (its jobs listing already returns these), the
  paginating adapter (descriptions already inline), and the dark adapter (listing cards
  carry title/location/date).
- For large boards, the deterministic filter rejects the large majority of postings on
  listing-level fields (chiefly location and target-function), so the survivor set requiring
  description retrieval is small — consistent with observed runs.
- The exact placement of lazy description retrieval (in the fetch stage versus the scoring
  stage's filter-survivor set) is a plan-phase design decision; this spec requires only that
  descriptions are never retrieved for filter-rejected postings.
- The platform execution-window limit (~15 minutes, no in-execution retry) and the scheduled
  cadence are fixed constraints this feature does not change; raising the limit is explicitly
  not the primary remedy, because an unbounded source must be bounded regardless. The bound
  is enforced by lazy/filtered description retrieval (FR-003) as the primary mechanism plus
  the configurable fetch-stage safety backstop (FR-013) as a fail-loud guardrail.
- This feature extends resilience to the per-item level; it reuses, and does not replace, the
  existing run-level per-source containment and degraded-run reporting model.
- No destructive schema migration is required for already-ingested postings to remain
  scorable; if listing-first ingestion needs to represent "description not yet retrieved,"
  that representation must not require wiping or re-ingesting existing data.
- Persistent per-source configuration errors on specific misconfigured tenants (e.g. an
  unverified location-facet value yielding repeated 400s) are out of scope here; per-source
  and per-item resilience already keep them from harming other sources, and deeper
  remediation is a possible follow-up.
- Bounded staleness is achievable in steady state: the per-run fetch budget exceeds the
  per-run rate of newly appearing in-scope survivors, so a one-time backlog (e.g. a freshly
  added large board) drains over a bounded number of runs rather than growing unbounded. A
  source whose steady-state inflow structurally exceeds the per-run budget cannot converge
  and is surfaced as a persistent degradation (FR-015) for maintainer action — raise the
  budget, tighten the filter, or drop the source.
