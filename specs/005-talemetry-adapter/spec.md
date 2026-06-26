# Feature Specification: Talemetry / TTC-Portals ATS Adapter

**Feature Branch**: `005-talemetry-adapter`

**Created**: 2026-06-24

**Status**: Draft

**Input**: User description: "Add a fifth ATS adapter (after greenhouse,
lever, workday, icims) for careers sites fronted by the Talemetry / TTC-Portals
recruitment-marketing platform — the P2 'custom-portal scraper' in the
git-ignored plans/adapter-implementation-sequence.md. A single-target,
server-rendered HTML scraper (NOT a generic multi-tenant Talemetry adapter):
predictable `/jobs/{id}-{slug}/` listing and detail URLs where the numeric job
ID is a clean dedupe key. Configured in registry.toml by a `talemetry` vendor
key plus a `host` field; conforms to the existing
`fetch(slug, *, company=...) -> list[Posting]` contract; stub-based no-network
tests. No real target-employer name may appear in this feature's deliverable
artifacts — only the platform name and a placeholder host."

## Clarifications

### Session 2026-06-24

- Q: When a listing entry lacks the full description the scorer needs → A:
  Fetch the job's detail page (`/jobs/{id}-{slug}/`) for the description, like
  the Workday adapter; the per-employer cap and politeness sleep bound the cost.
- Q: How to treat a fetch that parses zero postings → A: Return an empty list
  but emit a distinct, visible warning — fail-visibly without false-alarming a
  genuinely empty board; not a hard failure.
- Q: How to parse the careers-page HTML → A: Use a dedicated third-party
  HTML-parsing dependency (not stdlib-only). This is the project's first heavy
  dependency beyond `anthropic`, so the plan's Constitution Check (IV —
  Simplicity & Stdlib-First) MUST record and justify it.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A Talemetry-hosted employer feeds the digest (Priority: P1)

The maintainer adds the target employer's Talemetry / TTC-Portals careers site
to the registry, and that employer's open US job postings start appearing in
the daily triaged digest, scored against the personal profile exactly like
postings from the Greenhouse, Lever, Workday, and iCIMS sources. The target is
a strong regional fit whose careers site is a Talemetry-fronted custom portal
rather than a standard ATS board, so without this adapter its openings are
invisible to the pipeline.

**Why this priority**: This is the entire value of the feature — it is the MVP.
Everything else (capping, graceful degradation) only matters once postings are
flowing. It is the locked P2 build in the implementation sequence, after
Workday (P0) and iCIMS (P1).

**Independent Test**: Wire the one target host into the registry and run the
fetch stage; the employer's open US postings are upserted as normalized
postings and become scoreable, with no manual per-posting handling.

**Acceptance Scenarios**:

1. **Given** a registry source naming the Talemetry-hosted careers host,
   **When** the fetch stage runs, **Then** the employer's open US postings are
   returned as normalized `Posting` records carrying the fields scoring and
   dedupe need.
2. **Given** the same host is fetched again on a later run, **When** an
   already-seen posting (same numeric job ID) reappears, **Then** the dedupe
   fingerprint prevents a duplicate row.
3. **Given** the source has a display-name set on its registry entry, **When**
   its postings reach the digest, **Then** they show that human-readable
   company name, not the raw host.

---

### User Story 2 - A large board stays bounded (Priority: P2)

A large Talemetry careers board does not flood the local store or inflate
scoring spend: the existing per-run per-employer cap limits how many postings
the source contributes per run.

**Why this priority**: Protects Constitution Principle I (cost discipline) and
VII (LLM spend efficiency). It reuses the global cap already honored by the
Workday and iCIMS adapters, so it is a wiring concern rather than new
machinery, but it must be honored or one big board can blow the budget on a
cold start.

**Independent Test**: Configure a small per-employer cap, fetch a host whose
board exceeds it, and confirm the adapter stops at the cap without paging past
it.

**Acceptance Scenarios**:

1. **Given** `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` is set and the board has more
   open postings than the cap, **When** the adapter fetches it, **Then** it
   returns no more than the cap and does not page past it.
2. **Given** the cap is unset, **When** the adapter fetches, **Then** it
   behaves like the other adapters' default (no artificial cap beyond what the
   source returns).

---

### User Story 3 - A failing source does not break the run (Priority: P3)

If the Talemetry source is unreachable or returns something unexpected, the
daily run still delivers the digest from every source that succeeded, and the
degradation is visible rather than silent.

**Why this priority**: Constitution Principle V (fail loud, fail visibly) and
the established per-source resilience of the existing adapters. Lower priority
only because it is a robustness guarantee on top of the core capability.

**Independent Test**: Simulate the source raising a network/parse error and
confirm the overall fetch continues and surfaces the failure.

**Acceptance Scenarios**:

1. **Given** the Talemetry source errors during fetch, **When** the fetch stage
   runs across multiple sources, **Then** the other sources still produce
   postings and the digest is delivered.
2. **Given** the source returns malformed or empty HTML, **When** the adapter
   parses it, **Then** it fails for that source without raising an unhandled
   exception that aborts the whole run.

### Edge Cases

- What happens when the board returns zero open postings? (Expect an empty
  list, not an error.)
- How does the system handle a listing entry whose URL has no parseable numeric
  job ID? (That ID is the dedupe key, so the entry must be skipped with a
  visible warning rather than producing an unstable fingerprint.)
- How does the system handle a posting whose listing entry omits the
  description? Resolved: the adapter fetches the job's detail page for the
  description (FR-013) rather than excluding the posting — the scorer needs
  description text.
- How does the system handle a posting with no parseable location? (Retained
  and passed to scoring, not dropped — the deterministic gate excludes only
  locations it can positively identify as non-US, matching the existing
  keep-if-any filter philosophy.)
- What happens at the end of pagination, or when the page layout changes such
  that no postings parse? Resolved: a zero-posting parse returns an empty list
  but emits a distinct, visible warning (FR-014), so a broken scrape is
  noticeable without false-alarming a genuinely empty board.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST provide a Talemetry / TTC-Portals adapter that, given
  a registry source identifying a Talemetry-hosted careers host, returns the
  target employer's open US job postings as normalized `Posting` records.
- **FR-002**: The adapter MUST be selectable from `registry.toml` via a
  `talemetry` `vendor` value and dispatched through the same fetch registry as
  `greenhouse`/`lever`/`workday`/`icims`, with no change to the
  `fetch(slug, *, company=...) -> list[Posting]` contract.
- **FR-003**: Each returned posting MUST carry the fields the scorer and the
  dedupe fingerprint require: company, title, location, description text, a
  canonical posting URL, and the posting date where the source exposes it.
- **FR-004**: The adapter MUST use the numeric job ID parsed from the
  `/jobs/{id}-{slug}/` URL as the posting's stable external identifier, so the
  dedupe fingerprint survives slug/title edits and re-crawls.
- **FR-005**: The adapter MUST restrict results to US-based postings consistent
  with the existing adapters: only locations positively identified as non-US
  are excluded before scoring; a posting with an unparseable or ambiguous
  location is retained and passed to scoring (Constitution VII — filter before
  you spend, but never falsely reject).
- **FR-006**: The adapter MUST honor the existing global per-run per-employer
  cap (`JOBAGENT_MAX_POSTINGS_PER_EMPLOYER`) exactly as the Workday and iCIMS
  adapters do, so a large board cannot flood the store or scoring budget.
- **FR-007**: A transient failure of the Talemetry source (network error,
  unexpected/empty/malformed response) MUST NOT abort the run; the pipeline
  continues with the sources that succeeded and surfaces the degradation.
- **FR-008**: The adapter MUST apply politeness rate-limiting consistent with
  the existing adapters (a sleep between paginated and detail-page requests) so
  it does not hammer the source.
- **FR-009**: The company display name MUST resolve through the registry's
  existing `name` field, falling back to the source host when `name` is absent,
  since company feeds the dedupe fingerprint and MUST never be empty.
- **FR-010**: The feature MUST be scoped to a single target careers host. A
  lightweight platform-detection signal (recognizing `talemetry`/`ttcportals`
  markers in careers-page HTML or URLs) MAY be included to enable future reuse,
  but the adapter MUST NOT be generalized into a multi-tenant Talemetry adapter
  in this feature.
- **FR-011**: Tests MUST be stub-based with no network calls, following the
  existing patterns in `tests/`.
- **FR-012**: No real target-employer name may appear in this feature's
  deliverable artifacts (this spec, the adapter, its tests, or
  `registry.toml.example`). Committed examples MUST use a placeholder host such
  as `careers.example.com`; the real host lives only in the git-ignored
  `registry.toml`. Scope note: this governs the deliverables listed here, not
  the repo's pre-existing strategy notes under `plans/`, which may name real
  targets.
- **FR-013**: When a listing entry omits the full description, the adapter MUST
  fetch that job's detail page (`/jobs/{id}-{slug}/`) to obtain the description
  rather than excluding the posting. These detail fetches are bounded by the
  per-employer cap (FR-006) and the politeness sleep (FR-008).
- **FR-014**: When a fetch parses zero postings, the adapter MUST return an
  empty list and emit a distinct, visible warning in the run output, treated as
  a successful-but-suspicious result rather than a hard failure — so a broken
  scrape is noticeable without false-alarming a genuinely empty board
  (Constitution V).
- **FR-015**: HTML parsing uses a dedicated third-party HTML-parsing
  dependency. Adding it is an explicit, blessed exception to the stdlib-first
  default (Constitution IV — `anthropic` is otherwise the only heavy
  dependency); the plan MUST record the dependency and its justification in its
  Constitution Check, keeping the addition minimal.

### Key Entities *(include if feature involves data)*

- **Posting** (existing): the normalized job posting the whole pipeline shares —
  source, company, external id, title, location, description, canonical URL,
  posting date — plus the dedupe fingerprint derived from it.
- **Talemetry registry source**: identifies one Talemetry-hosted careers site,
  encoded as a `[[source]]` table in `registry.toml` with `vendor = "talemetry"`
  and a `host` field (the careers domain), plus the optional `name` and
  `enabled` fields common to all sources. The reconstructed slug passed to the
  adapter is the host.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: After wiring the target host, its open US postings appear in the
  next digest alongside the Greenhouse/Lever/Workday/iCIMS sources, with no
  manual per-posting steps.
- **SC-002**: For a board whose open-posting count exceeds the configured cap, a
  single run fetches no more than `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` postings
  from it.
- **SC-003**: When the Talemetry source is unreachable, the digest is still
  delivered from the remaining sources and the failure is visible in the run
  output.
- **SC-004**: A posting seen on two consecutive runs produces exactly one stored
  row — the numeric-ID-based fingerprint is stable across re-crawls.
- **SC-005**: `uv run pytest` passes with the adapter covered by stub-based
  tests that exercise listing parse, detail/description handling, US filtering,
  the cap, dedupe, and source-failure handling.
- **SC-006**: A reviewer can grep the committed diff and find no real
  target-employer name — only the platform name and the placeholder host.
- **SC-007**: A run that parses zero postings shows a distinct warning in the
  output (distinguishable from a genuinely empty board) and does not mark the
  source as failed.

## Assumptions

- The target careers site is server-rendered HTML reachable without
  authentication, with listing and detail pages following the
  `/jobs/{id}-{slug}/` pattern. The exact HTML structure (selectors, pagination
  mechanism, where the description lives) is a recon item resolved in the plan's
  research phase and confirmed against the live site before any selector or
  constant is hardcoded (carried forward from the Workday/iCIMS "verify live
  before the commit gate" lesson).
- The numeric job ID in the URL is a stable dedupe key. When a listing entry
  omits the full description, the adapter fetches the job's detail page (decided
  in FR-013, as in the Workday adapter), governed by the per-employer cap and
  the politeness sleep.
- HTML parsing uses a third-party parsing dependency (decided in FR-015) — the
  project's first heavy dependency beyond `anthropic`. The plan's Constitution
  Check (IV) must record and justify it, and `uv.lock`/`pyproject.toml` gain the
  new dependency as a committable change.
- US-only scoping mirrors the existing adapters; with no server-side US facet on
  a scraped HTML board, deterministic client-side location filtering before
  scoring is acceptable.
- The single colon-free `host` token needs no compound-slug encoding; registry
  reconstruction returns the host unchanged, so the `fetch(slug, ...)` contract
  stays unchanged.
- The global cap env var `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` already exists and
  is reused as-is, not redefined.
- The registry `name` field (2.0.0) supplies the display company; the retired
  `companies.toml` mapping is not reintroduced.
- The per-source `max_per_employer` registry override is reserved and not
  consumed by any adapter today; wiring it is out of scope for this feature.
- Generic multi-tenant Talemetry support and the other deferred bespoke custom
  portals are out of scope.
- Wiring the actual host (and optional display name) into `registry.toml` is a
  local, git-ignored step after the adapter is green, not part of the
  committable diff.
