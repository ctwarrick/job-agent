# Feature Specification: iCIMS ATS Adapter

**Feature Branch**: `003-icims-adapter`

**Created**: 2026-06-22

**Status**: Draft

**Input**: User description: "iCIMS ATS adapter — add an `adapters/icims.py`
exposing `fetch(slug) -> list[Posting]` (same contract as
greenhouse/lever/workday), registered as \"icims\" in fetch.py's ADAPTERS
table. Pulls open US postings from iCIMS-hosted career portals. Reuses the
existing per-employer cap (JOBAGENT_MAX_POSTINGS_PER_EMPLOYER). Confirmed
tenants and later verify-then-wire targets are listed in the git-ignored
plans/adapter-implementation-sequence.md (Phase 2). Open recon question:
confirm the JSON/portal endpoint shape on the two confirmed tenants and whether
`careers-{co}.icims.com` vs `jobs.{co}.com`
hosting changes the call shape. Follow the stub-based, no-network test pattern.
registry.txt and companies.toml stay git-ignored. Context:
plans/adapter-implementation-sequence.md Phase 2."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - iCIMS-hosted employers feed the digest (Priority: P1)

The maintainer adds an iCIMS-hosted employer to the watch list and that
employer's open US job postings start appearing in the daily triaged digest,
scored against the personal profile exactly like postings from Greenhouse,
Lever, and Workday sources. Several target employers in the priority regions (the confirmed and
verify-then-wire targets named in the git-ignored
plans/adapter-implementation-sequence.md) run their careers sites on iCIMS, so
without this adapter their openings are invisible to the pipeline.

**Why this priority**: This is the entire value of the feature — it is the
MVP. Everything else (capping, graceful degradation) only matters once
postings are flowing. iCIMS is the locked P1 adapter in the implementation
sequence after Workday (P0).

**Independent Test**: Wire one confirmed iCIMS employer into the registry and
run the fetch stage; the employer's open US postings are upserted as normalized
postings and become scoreable, with no manual per-posting handling.

**Acceptance Scenarios**:

1. **Given** a registry entry naming an iCIMS-hosted employer, **When** the
   fetch stage runs, **Then** that employer's open US postings are returned as
   normalized `Posting` records carrying the fields scoring and dedupe need.
2. **Given** the same employer is fetched again on a later run, **When** an
   already-seen posting reappears, **Then** the dedupe fingerprint prevents a
   duplicate row.
3. **Given** an iCIMS employer with a display-name mapping, **When** its
   postings reach the digest, **Then** they show a human-readable company name,
   not the raw tenant slug.

---

### User Story 2 - Large iCIMS boards stay bounded (Priority: P2)

A large iCIMS career board (some confirmed tenants are big national employers)
does not flood the local store or inflate scoring spend: the per-run
per-employer cap limits how many postings any single iCIMS tenant contributes
per run.

**Why this priority**: Protects Constitution Principle I (cost discipline) and
VII (LLM spend efficiency). It reuses the cap already introduced with the
Workday adapter, so it is a wiring concern rather than new machinery, but it
must be honored or a single big board can blow the budget on a cold start.

**Independent Test**: Configure a small per-employer cap, fetch a tenant whose
board exceeds it, and confirm the adapter stops at the cap.

**Acceptance Scenarios**:

1. **Given** `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` is set and an iCIMS board has
   more open postings than the cap, **When** the adapter fetches it, **Then**
   it returns no more than the cap and does not page past it.
2. **Given** the cap is unset, **When** the adapter fetches, **Then** it
   behaves like the other adapters' default (no artificial cap beyond what the
   source returns).

---

### User Story 3 - A failing iCIMS tenant does not break the run (Priority: P3)

If one iCIMS tenant is unreachable or returns something unexpected, the daily
run still delivers the digest from every source that succeeded, and the
degradation is visible rather than silent.

**Why this priority**: Constitution Principle V (fail loud, fail visibly) and
the established per-source resilience of the existing adapters. Lower priority
only because it is a robustness guarantee on top of the core capability.

**Independent Test**: Simulate a tenant raising a network/parse error and
confirm the overall fetch continues and surfaces the failure.

**Acceptance Scenarios**:

1. **Given** one iCIMS tenant errors during fetch, **When** the fetch stage
   runs across multiple sources, **Then** the other sources still produce
   postings and the digest is delivered.
2. **Given** a tenant returns a malformed or empty response, **When** the
   adapter parses it, **Then** it fails for that tenant without raising an
   unhandled exception that aborts the whole run.

### Edge Cases

- What happens when an iCIMS tenant returns zero open postings? (Expect an
  empty list, not an error.)
- How does the system handle a posting with no parseable location? Resolved:
  assumed in scope and passed through to scoring, not dropped — the
  deterministic gate excludes only locations it can positively identify as
  non-US, matching the existing keep-if-any filter philosophy (over-include,
  let LLM scoring resolve the bleed).
- How does the system handle a posting whose listing entry omits the
  description? (Either a bounded per-posting detail fetch, or exclusion — the
  scorer needs description text.)
- What happens when the two hosting styles (`careers-{co}.icims.com` vs
  `jobs.{co}.com`) require different call shapes for different tenants?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST provide an iCIMS adapter that, given a registry entry
  identifying an iCIMS-hosted employer, returns that employer's open US job
  postings as normalized `Posting` records.
- **FR-002**: The adapter MUST be selectable from `registry.txt` via an
  `icims` source keyword and dispatched through the same fetch registry as
  `greenhouse`/`lever`/`workday`, with no change to the
  `fetch(slug) -> list[Posting]` contract.
- **FR-003**: Each returned posting MUST carry the fields the scorer and the
  dedupe fingerprint require: company, title, location, description text, a
  canonical posting URL, and the posting date where the source exposes it.
- **FR-004**: The adapter MUST restrict results to US-based postings,
  consistent with the existing adapters: only locations positively identified
  as non-US are excluded before scoring; a posting with an unparseable or
  ambiguous location is retained and passed to scoring (Constitution VII —
  filter before you spend, but never falsely reject).
- **FR-005**: The adapter MUST honor the existing per-run per-employer cap
  (`JOBAGENT_MAX_POSTINGS_PER_EMPLOYER`) so a large board cannot flood the
  store or scoring budget.
- **FR-006**: A transient failure of a single iCIMS tenant (network error,
  unexpected/empty response) MUST NOT abort the run; the pipeline continues
  with the sources that succeeded and surfaces the degradation.
- **FR-007**: The adapter MUST apply politeness rate-limiting consistent with
  the existing adapters so it does not hammer a tenant.
- **FR-008**: The company display name for an iCIMS tenant MUST resolve through
  the existing display-name mapping mechanism, falling back to the tenant slug
  when no mapping exists, since company feeds the dedupe fingerprint and MUST
  never be empty.
- **FR-009**: The feature MUST support the two confirmed tenants (named in the
  git-ignored plans/adapter-implementation-sequence.md); the later
  verify-then-wire targets are explicitly out of scope for this feature and
  handled by later wiring.
- **FR-010**: Tests MUST be stub-based with no network calls, following the
  existing patterns in `tests/`.
- **FR-011**: Personal runtime files (`registry.txt`, `companies.toml`,
  `jobs.db`) MUST stay git-ignored; only the adapter, its tests, and any
  example/template file are committable.

### Key Entities *(include if feature involves data)*

- **Posting** (existing): the normalized job posting the whole pipeline shares
  — company, title, location, description, canonical URL, posting date — plus
  the dedupe fingerprint derived from it.
- **iCIMS registry entry**: identifies one iCIMS-hosted employer and the
  information the adapter needs to locate its board, encoded as a single
  `registry.txt` line under the `icims` keyword (compound colon-slug if more
  than one identifier is required, mirroring the Workday convention).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: After wiring a confirmed iCIMS employer, that employer's open US
  postings appear in the next digest alongside the Greenhouse/Lever/Workday
  sources, with no manual per-posting steps.
- **SC-002**: For a tenant whose board exceeds the configured cap, a single run
  fetches no more than `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` postings from it.
- **SC-003**: When an iCIMS tenant is unreachable, the digest is still
  delivered from the remaining sources and the failure is visible in the run
  output.
- **SC-004**: Adding a second iCIMS employer requires only a registry line
  (plus an optional display-name entry) — no code change.
- **SC-005**: `uv run pytest` passes with the iCIMS adapter covered by
  stub-based tests that exercise list parsing, US filtering, the cap, dedupe,
  and per-tenant failure handling.

## Assumptions

- The iCIMS-hosted portals for the confirmed tenants expose a machine-readable
  postings listing reachable without authentication. The exact endpoint shape,
  and whether `careers-{co}.icims.com` vs `jobs.{co}.com` hosting changes the
  call shape, is a recon item resolved in the plan's research phase and
  confirmed against the two live tenants before any tenant-specific constant is
  hardcoded (carried forward from the Workday "verify live before the commit
  gate" lesson).
- If the listing endpoint omits the full description, a bounded per-posting
  detail fetch (as in the Workday adapter) is acceptable, governed by the
  per-employer cap and the politeness sleep.
- US-only scoping mirrors the existing adapters; if iCIMS offers no server-side
  US location facet, deterministic client-side location filtering before
  scoring is acceptable.
- The compound colon-slug encoding is reused if an iCIMS entry needs more than
  one identifier, so `fetch.load_registry` and the `fetch(slug)` contract stay
  unchanged.
- `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` already exists (introduced with the
  Workday adapter) and is reused as-is, not redefined.
- The display-name mapping mechanism (`companies.toml`, git-ignored, with a
  committed example) already exists and is reused for iCIMS tenants.
- Wiring the actual confirmed employers into `registry.txt`/`companies.toml` is
  a local, git-ignored step after the adapter is green, not part of the
  committable diff.
