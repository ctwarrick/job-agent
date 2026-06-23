# Tasks: iCIMS ATS Adapter

**Feature**: `003-icims-adapter` | **Plan**: [plan.md](plan.md) | **Spec**: [spec.md](spec.md)

TDD is mandatory here (Constitution III + FR-010): test tasks precede
implementation within each story. Tests are stub-based, no network. The recon
spike (T001) is the single network step and is **dev-only / throwaway — never
committed**; it produces the real fixtures the stub tests run against, applying
the Workday retro lesson (verify the live external API before the gate; build
fakes from the real shape). Sibling reference: `adapters/workday.py` /
`tests/test_workday.py`.

## Phase 1: Setup

- [X] T001 Recon spike (dev-only, network; throwaway, NOT committed): for the
  two confirmed tenants named in the git-ignored
  `plans/adapter-implementation-sequence.md`, confirm each is iCIMS (detection
  hook: career-site HTML / redirects contain `icims.com`), resolve the real
  host (`{tenant}.icims.com` / `careers-*.icims.com` / a `jobs.{co}.com`
  custom domain), choose the access method (sitemap.xml + HTML detail,
  `/jobs/search?pr=` HTML, or an internal JSON endpoint), and capture one
  listing/sitemap response + one job-detail response per tenant. Record the
  confirmed method/host in `specs/003-icims-adapter/research.md`.
- [X] T002 Save the captured responses as test fixtures under
  `tests/fixtures/icims/` (or an inline fixtures module), following the
  stub-payload pattern in `tests/test_workday.py`. Depends on T001.
- [X] T003 [P] Extend `companies.toml.example` with a sample iCIMS
  tenant → display-name entry.

## Phase 2: Foundational (blocks all user stories)

- [X] T004 Create the adapter skeleton `src/job_agent/adapters/icims.py` with
  `def fetch(slug: str) -> list[Posting]` raising `NotImplementedError` and the
  internal slug-split helper signature, mirroring `adapters/workday.py`'s module
  shape (uniform `resp.raise_for_status()` + parse; no per-call duck-typing).
- [X] T005 Register `"icims": icims.fetch` in the `ADAPTERS` table in
  `src/job_agent/fetch.py`. Depends on T004.

## Phase 3: User Story 1 — iCIMS employers feed the digest (P1) — MVP

**Goal**: a confirmed iCIMS employer's open US postings flow into the pipeline
as normalized `Posting`s, scoreable like every other source.

**Independent test**: wire one confirmed tenant locally, run the fetch stage,
and confirm its open US postings upsert as `Posting`s (unparseable-location
postings retained, dedupe stable) with no manual per-posting handling.

### Tests (red) — author together, must fail for the right reason

- [X] T006 [US1] In `tests/test_icims.py`, test list/sitemap parsing → `Posting`
  records (company, title, location, description, url, numeric id) from the
  captured fixtures. Depends on T002, T004.
- [X] T007 [US1] Test the US-location filter: keeps US, drops
  positively-identified non-US, and **retains unparseable/ambiguous-location**
  postings (FR-004 resolution).
- [X] T008 [US1] Test the dedupe fingerprint is stable across two runs, keyed on
  the numeric job id.
- [X] T009 [US1] Test company display-name resolves via the `companies.toml`
  mapping and falls back to the tenant slug when absent (never empty).

### Implementation (green)

- [X] T010 [US1] Implement slug split + listing/sitemap fetch and parse →
  `Posting` in `src/job_agent/adapters/icims.py` (stdlib parsing per
  `research.md`). Depends on T006–T009.
- [X] T011 [US1] N/A per recon: the Jibe `/api/jobs` response carries the full
  description inline, so no second round-trip is needed (missing-description
  handling lands in US3/T019). Politeness sleep is between pages. Depends on T010.
- [X] T012 [US1] Implement the deterministic US-location filter
  (keep-if-any / retain-unparseable) before returning. Depends on T010.
- [X] T013 [US1] Implement display-name resolution from `companies.toml` with
  tenant-slug fallback. Depends on T010.
- [X] T014 [US1] `uv run pytest` until US1 tests pass; `uv run black
  --line-length 100` on `icims.py`, `test_icims.py`, `fetch.py`.

## Phase 4: User Story 2 — large iCIMS boards stay bounded (P2)

**Goal**: a single big board cannot flood the store or scoring budget.

**Independent test**: set a small cap, fetch a tenant whose board exceeds it,
confirm the adapter returns at most the cap and stops paging past it.

- [X] T015 [US2] In `tests/test_icims.py`, test
  `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` caps returned postings and halts
  pagination at the cap; unset → no artificial cap. (red) Depends on T002, T004.
- [X] T016 [US2] Implement cap-honoring in `icims.py` (read the env var, bound
  pagination + result count), reusing the cap convention from `workday.py`.
  (green) Depends on T010, T015.
- [X] T017 [US2] `uv run pytest`; confirm US2 tests green.

## Phase 5: User Story 3 — a failing iCIMS tenant does not break the run (P3)

**Goal**: one bad tenant degrades gracefully; the digest still ships from the
sources that succeeded (Constitution V).

**Independent test**: simulate a tenant raising a network/parse error; the
overall fetch continues and the failure is visible.

- [ ] T018 [US3] In `tests/test_icims.py`, test a single-tenant network/parse
  error is contained (returns empty / raises-caught, run not aborted) and a
  missing-description posting is excluded rather than scored empty. (red)
  Depends on T002, T004.
- [ ] T019 [US3] Implement contained per-tenant error handling + missing-
  description exclusion in `icims.py`. (green) Depends on T010, T018.
- [ ] T020 [US3] `uv run pytest`; confirm US3 tests green.

## Phase 6: Polish & cross-cutting

- [ ] T021 Full repo suite green: `uv run pytest`; `uv run black --line-length
  100 --check` on `icims.py`, `test_icims.py`, `fetch.py`.
- [ ] T022 Independent reviewer pass (fresh context: diff + plan only): re-run
  `uv run pytest`; confirm the spike-verified shape matches the committed
  fixtures, no unverified constant is hardcoded, and `icims.py` matches its
  sibling's shape (no `_unwrap`-style helper). No infra touched →
  `validate-infra.sh` N/A.
- [ ] T023 Local wiring (git-ignored, post-green, NOT committed): add the
  confirmed tenants' `icims` lines to `registry.txt` and display names to
  `companies.toml` (from the git-ignored plans doc); run
  `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER=10 uv run jobagent-fetch` to confirm
  upsert without error.
- [ ] T024 Human commit gate → releaser (1.2.0: CHANGELOG, version, `uv lock`,
  README) → retrospective.

## Dependencies & execution order

- **Setup** (T001–T003) before everything; T001 → T002 (fixtures need the
  spike). T003 is independent.
- **Foundational** (T004–T005) before any user story; T004 → T005.
- **US1** (T006–T014) is the MVP. Red tests T006–T009 before green T010–T013;
  T011–T013 depend on T010; T014 closes the story.
- **US2** (T015–T017) and **US3** (T018–T020) both build on the US1 core
  (T010); their red tests can be authored in the same red pass as US1's.
- **Polish** (T021–T024) after all stories; T024 is the human gate.

## Parallel opportunities

- T003 `[P]` runs alongside T001/T002.
- The red tests across stories (T006–T009, T015, T018) all live in
  `tests/test_icims.py` and are authored together in one test-writer red pass,
  shown red before any green work begins (project workflow: test-writer → all
  red, then implementer → all green).
- The green implementation tasks T011/T012/T013 touch distinct concerns within
  `icims.py` and can be done in sequence after T010 without cross-blocking.

## MVP scope

**User Story 1 (T001–T014)**: one confirmed iCIMS employer's open US postings
appear in the digest. US2 (cap) and US3 (resilience) harden it but are not
required for first value.

## Format validation

All 24 tasks use `- [ ] T### [P?] [US#?] description + file path`; Setup,
Foundational, and Polish carry no story label; US1–US3 tasks carry `[US1]`/
`[US2]`/`[US3]`; `[P]` marks only the cross-file-independent task (T003).
