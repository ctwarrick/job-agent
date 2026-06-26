---

description: "Task list for Talemetry / TTC-Portals ATS adapter"
---

# Tasks: Talemetry / TTC-Portals ATS Adapter

**Input**: Design documents from `specs/005-talemetry-adapter/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md),
[research.md](research.md), [data-model.md](data-model.md),
[contracts/talemetry-adapter.md](contracts/talemetry-adapter.md)

**Tests**: INCLUDED and required — Constitution III (Test-First Delivery,
NON-NEGOTIABLE) and FR-011 mandate stub-based, no-network tests authored red
before implementation. The repo's test-writer→implementer split owns this.

**Organization**: grouped by user story (US1 P1 → US2 P2 → US3 P3) for
independent implementation and testing. Most work concentrates in one new
module (`adapters/talemetry.py`), so cross-task parallelism is limited and
called out honestly below.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no incomplete dependency)
- **[Story]**: US1/US2/US3 for story-phase tasks only

## Path Conventions

Single project: `src/job_agent/`, `tests/` at repo root (per plan.md
Structure Decision).

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: install the one new dependency (the Constitution IV lead item)
before any module imports it.

- [X] T001 Add `beautifulsoup4>=4.12` to `[project].dependencies` in
  pyproject.toml (pure-Python `html.parser` backend; no `lxml` — see plan.md
  lead section)
- [X] T002 Run `uv lock` and `uv sync` to install beautifulsoup4 and commit
  the refreshed uv.lock

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: non-behavioral scaffolding so imports resolve and the test files
can load. No parsing/validation behavior here — that is TDD'd inside the
stories, so the story tests fail red against these stubs.

**⚠️ CRITICAL**: no user-story work can begin until this phase is complete.

- [X] T003 Create the adapter module skeleton in
  src/job_agent/adapters/talemetry.py: module docstring (why + data contract),
  module-level `import requests`, `import time`, `from bs4 import
  BeautifulSoup` (so tests can monkeypatch `talemetry.requests` /
  `talemetry.time.sleep`), and `fetch(slug, *, company=None, timeout=20) ->
  list[Posting]` returning `[]`
- [X] T004 Register the adapter in src/job_agent/fetch.py: add `talemetry` to
  the `from .adapters import ...` line and `"talemetry": talemetry.fetch` to
  `ADAPTERS` (depends on T003 — module must import)

**Checkpoint**: `from job_agent.adapters import talemetry` and a talemetry
`ADAPTERS` lookup resolve; registry still rejects talemetry sources (no
validation wiring yet), so US1 registry tests will fail red as intended.

---

## Phase 3: User Story 1 - A Talemetry-hosted employer feeds the digest (Priority: P1) 🎯 MVP

**Goal**: a registry-defined Talemetry host's open US postings come back as
normalized `Posting` records — listing parse, detail-page description fetch,
keep-if-any US filter, numeric-ID `external_id`, and registry slug/company
resolution — scoreable exactly like the other adapters.

**Independent Test**: wire a placeholder talemetry host into a test registry
and run the stubbed fetch; the employer's US postings return as normalized
`Posting`s with the required scorer/dedupe fields, and the display name
resolves from the registry `name` (falling back to `host`). Proven by
tests/test_talemetry.py and the talemetry cases in tests/test_registry.py.

### Tests for User Story 1 (write FIRST, ensure they FAIL) ⚠️

- [X] T005 [P] [US1] Adapter red tests in tests/test_talemetry.py (stub-based,
  mirror the `_FakeRequests`/`_FakeResponse` pattern from
  tests/test_workday.py, but with an HTML `.text` body): `_parse_job_id`
  parses the numeric id from `/jobs/{id}-{slug}/`; fetch builds the correct
  listing URL from the `host` slug; a listing parse maps through `normalize`
  to a `Posting` carrying source/company/external_id/title/location/
  description/url/posted_at (FR-003); the detail page is fetched for the
  description when the listing omits it (FR-013); the keep-if-any US gate drops
  a positively-non-US location and retains US/ambiguous/empty (FR-005);
  `company` comes from the kwarg and defaults to `host` when absent (FR-009); a
  politeness sleep occurs between requests (FR-008)
- [X] T006 [P] [US1] Registry red tests in tests/test_registry.py: a
  `vendor="talemetry"` source with `host` validates → reconstructed slug ==
  host and company resolves (name if set, else host); a talemetry source
  missing `host` fails loud (`ValueError`); an unrecognized key on a talemetry
  source fails loud

### Implementation for User Story 1

- [X] T007 [P] [US1] Wire talemetry into src/job_agent/registry.py:
  `_REQUIRED_FIELDS["talemetry"] = ("host",)`,
  `_VENDOR_FIELDS["talemetry"] = {"host"}`, a `_reconstruct_slug` branch
  returning `raw["host"]`, and a `_resolve_company` branch returning `name`
  else `raw["host"]` (different file from the adapter — parallel-safe)
- [X] T008 [US1] Implement the pure helpers in
  src/job_agent/adapters/talemetry.py: `_parse_job_id(href) -> str | None`
  (regex on `/jobs/{id}-{slug}/`) and `_is_us(location) -> bool` (keep-if-any:
  False only on a positively-non-US marker, else True), with named module
  constants for the placeholder selectors/markers flagged "confirm live before
  commit" (research Decisions 2–3)
- [X] T009 [US1] Implement the `fetch` body in
  src/job_agent/adapters/talemetry.py (depends on T008): GET the listing, parse
  entries with BeautifulSoup, derive `external_id`/canonical URL via
  `_parse_job_id`, fetch the detail page for the description when absent
  (FR-013), apply `_is_us` before inclusion (FR-005), map through
  `schema.normalize`, page until entries are exhausted, and `time.sleep`
  between requests (FR-008)
- [X] T010 [P] [US1] Add a `talemetry` `[[source]]` block to
  registry.toml.example using the placeholder host `careers.example.com` and a
  placeholder `name`, with a header comment matching the existing per-vendor
  blocks (FR-012; no real employer name)

**Checkpoint**: US1 fully functional — a stubbed talemetry host yields
normalized US postings and the registry names it. MVP deliverable.

---

## Phase 4: User Story 2 - A large board stays bounded (Priority: P2)

**Goal**: the existing global per-employer cap limits how many postings the
Talemetry source contributes per run, bounding store growth and scoring spend
(Constitution I/VII).

**Independent Test**: set `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` below a stubbed
board's size → fetch returns no more than the cap and does not page past it;
unset → all parsed postings return.

### Tests for User Story 2 (write FIRST, ensure they FAIL) ⚠️

- [X] T011 [US2] Cap red tests appended to tests/test_talemetry.py (same file
  as T005, so sequential): with `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` set below
  the board size, fetch returns exactly the cap and stops paging (no further
  listing/detail requests past the cap, mirroring
  test_workday `test_per_employer_cap_limits_results`); with the cap unset, all
  postings return (FR-006)

### Implementation for User Story 2

- [X] T012 [US2] Wire the `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` cap into the
  fetch loop in src/job_agent/adapters/talemetry.py (depends on T009): read the
  env cap like icims/workday, stop accumulating and stop paging once the cap is
  reached, and return `postings[:cap]`

**Checkpoint**: US1 + US2 both work — bounded fetch on a large board.

---

## Phase 5: User Story 3 - A failing source does not break the run (Priority: P3)

**Goal**: a Talemetry source that errors or returns nothing parseable degrades
visibly without aborting the run (Constitution V; FR-007/FR-014).

**Independent Test**: simulate the source raising an HTTP error → it propagates
to `fetch.main`'s per-source guard and the run continues; a zero-posting parse
returns `[]` with a distinct warning; a listing entry with no parseable numeric
ID is skipped with a warning while the rest are retained.

### Tests for User Story 3 (write FIRST, ensure they FAIL) ⚠️

- [X] T013 [US3] Resilience red tests appended to tests/test_talemetry.py:
  an HTTP error from the source raises out of `fetch` so `fetch.main` records
  the failure (FR-007, mirroring test_workday
  `test_fetch_raises_on_http_error...`); a parse yielding zero postings returns
  `[]` and emits a distinct stderr warning, captured via `capsys`, without
  raising (FR-014, SC-007); a listing entry whose URL has no parseable numeric
  ID is skipped with its own warning while valid entries are still returned

### Implementation for User Story 3

- [X] T014 [US3] Implement the fail-visible paths in
  src/job_agent/adapters/talemetry.py (depends on T009): a distinct
  `print(..., file=sys.stderr)` warning + `return []` on a zero-posting parse,
  a per-entry warning + skip when `_parse_job_id` returns `None`, and confirm
  transport errors are left to propagate (no broad `except`)

**Checkpoint**: all three stories independently functional.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T015 [P] Run `uv run pytest` and confirm the full suite is green,
  including the new tests (SC-005)
- [X] T016 [P] Privacy grep on the staged diff: no real employer name appears,
  and `careers.example.com` is the only host in registry.toml.example (FR-012,
  SC-006)
- [~] T017 SKIPPED (live target unreachable). The intended host fronts its
  board with a Cloudflare "managed challenge" — every path (listing, robots,
  sitemap, /api guesses) returns HTTP 403 + the JS interstitial regardless of
  User-Agent, so the `requests` + BeautifulSoup fetch cannot reach live markup
  and the placeholder selectors / `_NON_US_MARKERS` cannot be confirmed.
  Decision (2026-06-26): ship the capability DARK — committed and stub-green,
  but no live source wired into `registry.toml`. Revisit (headless browser or
  an alternate non-gated feed) only if the target's value is demonstrated.
- [X] T018 [P] Docstring/README drift check: the talemetry module docstring
  states the why + data contract like the other adapters, and README/docs that
  enumerate supported vendors include talemetry

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies — start immediately
- **Foundational (Phase 2)**: depends on Setup — BLOCKS all stories
- **User Stories (Phase 3–5)**: depend on Foundational; US2 and US3 layer
  behavior into the same `talemetry.py` that US1 creates, so they are
  sequenced after US1's `fetch` body (T009), not truly parallel
- **Polish (Phase 6)**: depends on the desired stories being complete

### User Story Dependencies

- **US1 (P1)**: starts after Foundational; no dependency on other stories
- **US2 (P2)**: independently testable, but its impl (T012) extends the US1
  fetch loop (T009) — sequence after US1
- **US3 (P3)**: independently testable, but its impl (T014) extends the US1
  fetch loop (T009) — sequence after US1

### Within Each Story

- Tests authored and failing before implementation (Constitution III)
- Helpers (T008) before the fetch loop (T009)
- Cap (T012) and resilience (T014) after the fetch loop (T009)

### Parallel Opportunities

Limited by single-module concentration. Genuinely parallel:

- T005 (tests/test_talemetry.py) ∥ T006 (tests/test_registry.py) — different
  files
- T007 (registry.py) ∥ the talemetry.py impl (T008/T009) ∥ T010
  (registry.toml.example) — different files
- T015, T016, T018 in Polish — independent checks

NOT parallel: every task touching `talemetry.py` (T003, T008, T009, T012,
T014) and every task appending to `tests/test_talemetry.py` (T005, T011, T013)
— same file, must serialize.

---

## Parallel Example: User Story 1

```bash
# Red tests in different files, together:
Task: "Adapter red tests in tests/test_talemetry.py"      # T005
Task: "Registry red tests in tests/test_registry.py"      # T006

# Then implementation across different files, together:
Task: "Wire talemetry into src/job_agent/registry.py"     # T007
Task: "Add talemetry block to registry.toml.example"      # T010
# (talemetry.py helpers + fetch loop, T008→T009, run on their own thread)
```

---

## Implementation Strategy

### MVP First (User Story 1 only)

1. Phase 1 Setup → 2. Phase 2 Foundational → 3. Phase 3 US1 →
4. **STOP and VALIDATE**: a stubbed talemetry host yields normalized US
postings and the registry names it (the entire point of the feature).

### Incremental Delivery

US1 (MVP) → US2 (bounded large board) → US3 (fail-visible) → Polish, each a
green-suite increment that does not break the previous.

---

## Notes

- [P] = different files, no incomplete dependency; [Story] = traceability
- Verify each story's tests fail before implementing it
- Selectors/markers are UNCONFIRMED placeholders — T017 is SKIPPED (live target
  Cloudflare-gated), so the capability ships dark; a passing suite is NOT proof
  the live scrape works
- The dedupe key stays content-based in `schema.py`; `external_id` is the
  numeric ID but is NOT the fingerprint (plan.md "dedupe identity
  reconciliation") — do not author a test asserting an external_id fingerprint
- Wiring the real host into git-ignored `registry.toml` is a local post-green
  step, outside the committable diff
