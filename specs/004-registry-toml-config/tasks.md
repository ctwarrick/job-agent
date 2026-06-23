---
description: "Task list for Structured TOML source registry"
---

# Tasks: Structured TOML source registry

**Input**: Design documents from `/specs/004-registry-toml-config/`
**Prerequisites**: plan.md (approved), spec.md, data-model.md, contracts/registry-schema.md, quickstart.md

**Tests**: INCLUDED and RED-FIRST. The spec success criteria require new loader
tests "authored red-first," and the repo's constitution mandates TDD (failing
tests shown before implementation). Every implementation task is preceded by its
failing test task within the same slice.

**Organization**: This feature is specified as functional requirements (FR-001..
FR-007) rather than prioritized user stories. Tasks are grouped into three
independently-testable slices that map to those FRs:

- **US1 (P1, MVP)** — Validated TOML loader returning resolved `Source` records
  (FR-001, FR-003, FR-004, company resolution). Independently testable via
  `tests/test_registry.py`.
- **US2 (P2)** — Wire the resolved company through `fetch.main()` + all four
  adapters (`company` kwarg) and retire `companies.toml` + `_resolve_company`
  (FR-002, FR-007). Depends on US1's `Source`.
- **US3 (P3)** — Packaging, fingerprint-stability verification, and cutover prep
  (FR-005, FR-006). Depends on US1 + US2.

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1 / US2 / US3 (Setup/Foundational/Polish carry no story label)
- Exact file paths are included in each description.

## Path Conventions

Single-project CLI: source in `src/job_agent/`, tests in `tests/`, runtime
config + `.example` schema docs at the repo root.

> **Scope note (plan vs. stale spec line):** the approved (revised) plan and the
> handoff document a maintainer scope expansion — `fetch.main()` and all four
> adapters DO change this feature (they gain a `company` kwarg). The spec review
> checkbox "Adapter modules and `fetch.main()` unchanged" (spec.md:89) is stale
> from the pre-expansion first draft; the plan governs. US2 implements the change.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Confirm the working environment; no new project scaffolding.

- [X] T001 Confirm branch `004-registry-toml-config` is checked out and that no
  new dependencies are needed — `tomllib` and `dataclasses` are stdlib, so
  `pyproject.toml` and `uv.lock` stay unchanged (Principle IV stdlib-first).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that must exist before dependent slices.

**No foundational-only tasks.** The US1 loader (`registry.py` + the `Source`
dataclass) *is* the shared foundation that US2 and US3 build on; it is delivered
as the P1/MVP slice rather than split out, to keep its TDD red→green cycle
intact. US2/US3 depend on US1 completing (see Dependencies).

**Checkpoint**: Proceed to US1.

---

## Phase 3: User Story 1 — Validated TOML loader (Priority: P1) 🎯 MVP

**Goal**: A new `src/job_agent/registry.py` reads `registry.toml` via stdlib
`tomllib`, validates fail-loud, and returns ordered, resolved `Source`
records (vendor, reconstructed slug, resolved company) — mirroring `filter.py`.

**Independent Test**: `uv run pytest tests/test_registry.py` is green, and
`load_registry('registry.toml')` returns 14 `Source` records with
`Source(vendor='greenhouse', slug='stripe', company='stripe')`-shaped output
(quickstart §2); a malformed config raises `ValueError` naming the bad source
(quickstart §3).

### Tests for User Story 1 (RED FIRST) ⚠️

> Write these FIRST and prove they FAIL (module does not yet exist) before T003.

- [X] T002 [US1] Author `tests/test_registry.py` with the 8 stub-based,
  no-network tests from plan §TDD and prove red: (1) loads each vendor,
  reconstructs slug, resolves company (`name` → vendor default); (2) `enabled =
  false` omitted; (3) raises on unknown vendor; (4) raises on missing required
  field (e.g. workday `host`); (5) raises on duplicate `(vendor, slug)`; (6)
  raises on an unrecognized key (typo guard); (7) honors `JOBAGENT_DATA_DIR`;
  (8) inline `#` comments ignored. Follow existing `tests/` stub patterns.

### Implementation for User Story 1

- [X] T003 [US1] Create `src/job_agent/registry.py` to make T002 green: a
  module docstring stating the why + data contract; a frozen `Source`
  dataclass (`vendor: str`, `slug: str`, `company: str`); and
  `load_registry(path: str | None = None) -> list[Source]` that —
  (a) resolves `store.data_path("registry.toml")` (honors `JOBAGENT_DATA_DIR`)
  and `tomllib.load`s it;
  (b) validates each `[[source]]` fail-loud (vendor in `fetch.ADAPTERS`;
  required per-vendor fields non-empty per data-model.md; no unrecognized keys;
  `enabled` bool / `max_per_employer` int if present);
  (c) skips `enabled = false`;
  (d) reconstructs slug (gh/lever → `slug`; workday → `f"{tenant}:{site}:{host}"`;
  icims → `tenant`, or `f"{tenant}:{host}"` when `host` set);
  (e) resolves company (`name` if present, else `slug` for gh/lever / `tenant`
  for workday/icims);
  (f) raises `ValueError` naming the offending source on a duplicate
  `(vendor, slug)`. Black, line length 100, Google-style docstring.

**Checkpoint**: US1 loader is independently functional and testable.

---

## Phase 4: User Story 2 — Wire company through fetch + adapters; retire companies.toml (Priority: P2)

**Goal**: `fetch.main()` iterates `load_registry()` and passes the resolved
`company` to each adapter (new `company` kwarg); the duplicated
`_resolve_company` helpers, the `companies.toml` reads, and `companies.toml`
itself (+ its `.example`) are removed (FR-002, FR-007).

**Independent Test**: `uv run pytest tests/test_fetch.py tests/test_workday.py
tests/test_icims.py` green — failure records carry the `Source` vendor/slug, the
adapter receives `company`, and adapters default `company` to slug (gh/lever) /
tenant (workday/icims) with no `companies.toml` involvement.

### Tests for User Story 2 (RED FIRST) ⚠️

> Write/adjust these FIRST and prove they FAIL against the old contract.

- [ ] T004 [P] [US2] In `tests/test_fetch.py`: drop the 4 `registry.txt`
  parsing tests; update the failure-record tests to build `registry.Source`
  objects and assert the adapter is called with `company=source.company`. Prove red.
- [ ] T005 [P] [US2] In `tests/test_workday.py`: replace the 2 `companies.toml`
  resolution tests (~lines 294, 301) with "company comes from the `company`
  arg; defaults to tenant when absent." Prove red.
- [ ] T006 [P] [US2] In `tests/test_icims.py`: replace the 2 `companies.toml`
  resolution tests (~lines 197, 202) with the same "company from arg / defaults
  to tenant" pair. Prove red.

### Implementation for User Story 2

- [ ] T007 [US2] In `src/job_agent/fetch.py`: import `load_registry`; change the
  loop to `for source in load_registry():`, dispatch
  `ADAPTERS[source.vendor](source.slug, company=source.company)`, and build
  failure records from `source.vendor` / `source.slug`; drop the `registry.txt`
  read; update the module docstring (registry.txt → registry.toml). Depends on T003.
- [ ] T008 [P] [US2] In `src/job_agent/adapters/greenhouse.py`: add
  `company: str | None = None` to `fetch(...)` and use `company = company or slug`.
- [ ] T009 [P] [US2] In `src/job_agent/adapters/lever.py`: same `company` kwarg,
  `company = company or slug`.
- [ ] T010 [P] [US2] In `src/job_agent/adapters/workday.py`: add the `company`
  kwarg (`company = company or tenant`); delete `_resolve_company`, the
  `companies.toml` read, and `import tomllib` (plan refs L31, L69, L104); drop
  the companies.toml paragraph from the module docstring.
- [ ] T011 [P] [US2] In `src/job_agent/adapters/icims.py`: same as T010
  (plan refs L35, L68, L120).
- [ ] T012 [US2] Retire `companies.toml`: delete the committed
  `companies.toml.example`, and remove the `companies.toml` line from
  `.gitignore`. (The runtime git-ignored `companies.toml` is removed from the
  Azure Files share at cutover — see US3.)

**Checkpoint**: US1 + US2 both green; no `companies.toml` references remain in code.

---

## Phase 5: User Story 3 — Packaging, fingerprint stability & cutover prep (Priority: P3)

**Goal**: A committed schema doc (`registry.toml.example`), the git-ignored
populated `registry.toml`, and a verified fingerprint-stable resolution so the
file can be uploaded today without re-surfacing the whole backlog (FR-005,
FR-006).

**Independent Test**: `load_registry('registry.toml')` returns 14 sources; each
workday/icims `company` equals its former `companies.toml` display string and
gh/lever `company` equals the slug; no personal data appears in any committed
file.

- [ ] T013 [US3] Verify/finalize the committed `registry.toml.example` against
  contracts/registry-schema.md — illustrative public slugs only (greenhouse +
  workday + icims examples, a `# enabled = false` comment), no real company list
  (FR-006). (File already created; confirm contents.)
- [ ] T014 [US3] Confirm `.gitignore` contains `registry.toml` so the populated
  runtime file is never committed (FR-006). (Already added per working tree;
  verify.)
- [ ] T015 [US3] Verify the populated, git-ignored `registry.toml`: parses to
  exactly 14 sources (quickstart §2) and is fingerprint-stable (FR-005) — every
  workday/icims `name` equals the exact former `companies.toml` display string,
  gh/lever omit `name` so company falls back to the slug; then run the fail-loud
  check (quickstart §3). Do NOT quote real company names in any artifact.
- [ ] T016 [US3] End-to-end dry run: `DIGEST_DRY_RUN=1 uv run jobagent-fetch`
  emits one line per source across all four vendors (quickstart §4). No email sent.

**Checkpoint**: All slices functional; `registry.toml` ready for the sanctioned
manual cutover (quickstart §5 — operational, outside this build).

---

## Phase 6: Polish & Cross-Cutting Concerns

- [ ] T017 [P] Sweep for any remaining `registry.txt` / `companies.toml`
  references in module docstrings or comments outside the files changed above
  (e.g. `src/job_agent/` docstrings) and update them to `registry.toml`.
- [ ] T018 Run the full suite `uv run pytest` green; the reviewer re-runs it in
  fresh context. No infra (`infra/*.bicep*`) touched → `scripts/validate-infra.sh`
  not required for this feature.
- [ ] T019 [P] (Optional, non-blocking) Refresh Constitution II prose that names
  `registry.txt` by name to `registry.toml` in `.specify/memory/constitution.md`.
- [ ] T020 Run the full quickstart.md validation pass (§1–§4) as a final gate
  before the human approves commit/PR.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies.
- **Foundational (Phase 2)**: none (empty by design).
- **US1 (Phase 3)**: the shared foundation; start immediately after Setup.
- **US2 (Phase 4)**: depends on US1 (`registry.Source` must exist — T007 and the
  T004 test build `Source` objects).
- **US3 (Phase 5)**: depends on US1 (loader) and US2 (companies.toml retired so
  resolution flows only through `name`/defaults).
- **Polish (Phase 6)**: after US1–US3.

### Within Each Story (hard TDD gate)

- Test tasks are authored and shown RED before the implementation tasks in the
  same slice (T002 → T003; T004–T006 → T007–T012).
- No slice is "done" until `uv run pytest` passes; the reviewer re-runs it.

### Parallel Opportunities

- **US2 tests**: T004, T005, T006 touch three different test files → run in parallel.
- **US2 adapters**: T008, T009, T010, T011 touch four different adapter files →
  run in parallel once their red tests exist. T007 (`fetch.py`) is a separate
  file and may run alongside them, but is the integration point.
- T012 and T014 both touch `.gitignore` (remove vs. confirm a line) → NOT
  parallel with each other.
- Polish T017 / T019 touch different files → parallel.

---

## Parallel Example: User Story 2

```bash
# Red phase — three independent test files in parallel:
Task: "Update tests/test_fetch.py to the Source/company contract (T004)"
Task: "Replace companies.toml tests in tests/test_workday.py (T005)"
Task: "Replace companies.toml tests in tests/test_icims.py (T006)"

# Green phase — four adapter files in parallel:
Task: "Add company kwarg to src/job_agent/adapters/greenhouse.py (T008)"
Task: "Add company kwarg to src/job_agent/adapters/lever.py (T009)"
Task: "company kwarg + delete _resolve_company in src/job_agent/adapters/workday.py (T010)"
Task: "company kwarg + delete _resolve_company in src/job_agent/adapters/icims.py (T011)"
```

---

## Implementation Strategy

### MVP First (US1 only)

1. Phase 1 Setup → 2. US1 loader (T002 red → T003 green) → 3. **STOP & VALIDATE**:
   `tests/test_registry.py` green and `load_registry('registry.toml')` returns 14
   `Source` records. This alone is a demonstrable, fail-loud config loader.

### Incremental Delivery

1. US1 (loader) → validate independently.
2. US2 (wire company + retire `companies.toml`) → full `pytest` green.
3. US3 (packaging + fingerprint verification + dry run) → ready to upload.
4. Human gate → commit/PR → sanctioned manual cutover (quickstart §5).

---

## Notes

- Tests are stub-based with no network calls — follow existing `tests/` patterns.
- Black, line length 100; docstrings/full-line comments wrap at 80; Google-style
  docstrings with Args/Returns/Raises; type hints on all functions.
- **Never commit or quote** `registry.toml`, `companies.toml`, `profile.md`,
  `screening_prompt.md`, or `jobs.db` (personal data, git-ignored).
- The production drift root cause (a coverage/parity check) is explicitly out of
  scope here — separate workstream.
