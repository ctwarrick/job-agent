# Implementation Plan: Talemetry / TTC-Portals ATS Adapter

**Branch**: `005-talemetry-adapter` | **Date**: 2026-06-24 | **Spec**:
[spec.md](spec.md)

**Input**: Feature specification from
`specs/005-talemetry-adapter/spec.md`

## Constitution IV — Dependency Justification (lead item)

This feature adds **`beautifulsoup4`** — the project's first heavy dependency
beyond `anthropic` and `requests`, and the single deliberate deviation from
Constitution IV (Simplicity & Stdlib-First). It leads the plan because it is
the one decision that crosses a NON-NEGOTIABLE-adjacent line and was
explicitly flagged for the Constitution Check in the spec (FR-015, and the
2026-06-24 clarification session).

**What is added**: `beautifulsoup4>=4.12`, used with its built-in
`html.parser` backend. No `lxml`. Its only transitive dependency, `soupsieve`
(CSS-selector support), is likewise pure-Python.

**Why a third-party parser at all** (stdlib rejected): the four existing
adapters parse JSON the stdlib already handles; Talemetry careers pages are
server-rendered HTML with no JSON jobs API, so this is the project's first
genuine scrape. The stdlib `html.parser` is a low-level, event-driven
(`HTMLParser` subclass) parser with no tree navigation or selector model.
Extracting `/jobs/{id}-{slug}/` entries, locations, and descriptions through
it means hand-rolling stateful tag callbacks that re-break on every cosmetic
markup change — working directly against FR-005 (never falsely reject a
posting) and FR-014 (distinguish a broken scrape from a genuinely empty
board). BeautifulSoup gives a resilient tree/selector model that degrades to a
visible empty parse rather than a silent miss. The clarification session
weighed stdlib-only and rejected it for exactly this reason.

**Why `beautifulsoup4`, and why the `html.parser` backend** (`lxml` rejected):
`bs4` on the stdlib `html.parser` backend is pure-Python with no C extension
and no system library — it builds and ships cleanly into the consumption-based
Azure compute (Principle II) with zero binary-wheel risk. `lxml` is faster but
pulls in `libxml2`/`libxslt` native bindings, a heavier and more fragile
deploy footprint; its speed is irrelevant for a once-daily scrape of one
board. The minimal form of the exception is therefore `bs4` + `html.parser`.

**Why this stays minimal** (the rest of IV still holds): one adapter module of
small pure helpers (`_parse_job_id`, `_is_us`, `_parse_listing`) over classes;
one vendor key; single-target by FR-010 (explicitly **not** generalized into a
multi-tenant Talemetry adapter); subjective tuning (non-US markers, selectors)
stays in recon/runtime, not hardcoded business logic. The dependency is
recorded in the Complexity Tracking table below.

## Summary

Add a fifth ATS adapter, `talemetry`, for a single careers site fronted by the
Talemetry / TTC-Portals recruitment-marketing platform — the locked P2
"custom-portal scraper." It is a single-target, server-rendered HTML scraper
(not a generic multi-tenant Talemetry adapter): predictable
`/jobs/{id}-{slug}/` listing and detail URLs, with the numeric job ID parsed
from the URL as each posting's stable `external_id`. It conforms to the
existing `fetch(slug, *, company=...) -> list[Posting]` contract, is selected
from `registry.toml` by `vendor = "talemetry"` plus a `host` field, reuses the
global per-employer cap and the deterministic keep-if-any US filter, and is
covered by stub-based no-network tests. No real target-employer name appears in
any committed artifact — only the platform name and the placeholder host
`careers.example.com`. The lone new dependency (`beautifulsoup4`) is justified
in the lead section above.

## Technical Context

**Language/Version**: Python 3.12 (`requires-python = ">=3.12"`).

**Primary Dependencies**: `requests` (existing, HTTP); **`beautifulsoup4`
(new, HTML parse — see lead section)**; `anthropic` (existing, downstream
scoring, untouched here).

**Storage**: SQLite `jobs.db` via the existing `store.upsert_postings`
(`INSERT OR IGNORE` on the `fingerprint` PRIMARY KEY); no schema change.

**Testing**: `pytest`, stub-based, no network — `talemetry.requests` and
`talemetry.time.sleep` monkeypatched; canned HTML strings fed through the real
BeautifulSoup parser (parsing is deterministic and offline).

**Target Platform**: Linux — local dev and Azure consumption compute; the
pure-Python parser carries no binary-wheel deploy risk.

**Project Type**: single CLI/pipeline project (`src/job_agent/`); adapter
plug-in plus registry wiring.

**Performance Goals**: none beyond politeness — a once-daily batch with a sleep
between paginated and detail-page requests (FR-008); not latency-sensitive.

**Constraints**: results bounded by `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER`
(FR-006); no real employer identity in any committed artifact (FR-012,
SC-006); zero-posting parse warns rather than fails (FR-014).

**Scale/Scope**: one target host; one new adapter module, four-file wiring
(`fetch.ADAPTERS`, `registry.py`, `registry.toml.example`, `pyproject.toml` +
`uv.lock`), one new test module plus three registry-test additions.

## Constitution Check

*GATE: passed before Phase 0; re-checked after Phase 1 design (below).*

- **I. Cost Discipline** — PASS. No new cloud resources; runs inside the
  existing scheduled fetch stage. Adds only HTTP GETs (no LLM cost); detail
  fetches and stored rows are bounded by the per-employer cap. Expected
  monthly cloud-cost impact: **$0**. LLM scoring cost per posting is unchanged
  and bounded by the cap.
- **II. Cloud-Native Scheduled Operation** — PASS. No new infra; runs in the
  daily `main.py` path, runnable locally with env vars + git-ignored runtime
  files. The real host is wired only into the git-ignored `registry.toml`.
- **III. Test-First Delivery** — PASS (process gate). Stub-based no-network
  tests authored red before implementation; `uv run pytest` green before done;
  reviewer re-runs it. No infra touched, so `validate-infra.sh` is N/A.
- **IV. Simplicity & Stdlib-First** — PASS WITH JUSTIFICATION. The single
  deviation (`beautifulsoup4`) is justified in the lead section and recorded in
  Complexity Tracking; everything else is small pure functions, one module,
  one vendor key, single-target.
- **V. Fail Loud, Fail Visibly** — PASS. Zero-posting parse emits a distinct
  stderr warning (FR-014); an entry with no parseable numeric ID is skipped
  with a warning (edge case); a source-level failure is contained by
  `fetch.main`'s per-source `try/except` so the digest still ships (FR-007).
- **VI. Personal-Data Privacy** — PASS. Only the platform name and placeholder
  `careers.example.com` appear in committed artifacts; the real host lives in
  git-ignored `registry.toml` (FR-012, SC-006).
- **VII. LLM Spend Efficiency** — PASS. Deterministic, zero-cost US gate runs
  before scoring (filter before you spend); the per-employer cap bounds both
  detail fetches and postings sent downstream. The adapter itself makes no LLM
  call.

## Key design decision — dedupe identity reconciliation

FR-004 and SC-004 speak of a "numeric-ID-based fingerprint," but the shared
dedupe key in `schema.Posting.fingerprint` is deliberately **content-based**
(`title|company|location|description`) and `store.upsert_postings` dedupes on
it (`fingerprint` PRIMARY KEY, `INSERT OR IGNORE`); the `_migrate_fingerprints`
path even rebuilds rows with `external_id=""`, confirming `external_id` is not
part of dedupe identity. This plan reconciles the two as follows, and the
test-writer MUST honor it (do **not** author a test asserting an
`external_id`-based fingerprint — it would contradict `schema.py`):

- The numeric job ID parsed from `/jobs/{id}-{slug}/` is the posting's stable
  **`external_id`** (FR-004) and the basis for the canonical URL and the
  skip-if-unparseable rule.
- Cross-run dedupe (SC-004, US-1 scenario 2) is delivered by the existing
  content fingerprint, exactly as for every other adapter. The numeric-ID-keyed
  detail fetch makes the stored content deterministic, so the content
  fingerprint is stable across re-crawls. The fingerprint scheme is **not**
  changed for this adapter.

## Project Structure

### Documentation (this feature)

```text
specs/005-talemetry-adapter/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   └── talemetry-adapter.md   # Phase 1 output (adapter + registry contract)
├── checklists/
│   └── requirements.md  # pre-existing spec-quality checklist
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
src/job_agent/
├── adapters/
│   ├── greenhouse.py
│   ├── lever.py
│   ├── workday.py
│   ├── icims.py
│   └── talemetry.py     # NEW — fetch(slug, *, company=...) -> list[Posting]
├── fetch.py             # EDIT — import talemetry; ADAPTERS["talemetry"]
├── registry.py          # EDIT — _REQUIRED_FIELDS/_VENDOR_FIELDS/slug/company
├── schema.py            # unchanged (content fingerprint reused)
└── store.py             # unchanged

tests/
├── test_talemetry.py    # NEW — stub-based adapter tests
└── test_registry.py     # EDIT — talemetry validation/slug/company cases

registry.toml.example    # EDIT — add a talemetry [[source]] block
pyproject.toml           # EDIT — add beautifulsoup4 dependency
uv.lock                  # EDIT — re-locked (uv lock)
```

**Structure Decision**: single-project pipeline. The feature is one new
adapter module plus the four-file wiring every adapter needs (dispatch table,
registry validation, committed example, dependency manifest), matching how
`workday` and `icims` were added.

## Phase 0 — Research

See [research.md](research.md). Resolves the parser-backend choice (settled
above), the listing/pagination/detail recon plan, the US free-text location
gate, and the warning mechanism. The exact selectors and the non-US marker set
are placeholders confirmed live before the commit gate — the same "verify live
before commit" discipline applied to Workday's `USA_COUNTRY_WID`.

## Phase 1 — Design & Contracts

- [data-model.md](data-model.md) — the (unchanged) `Posting` entity, the
  Talemetry registry source, and the external_id/fingerprint mapping.
- [contracts/talemetry-adapter.md](contracts/talemetry-adapter.md) — the
  adapter `fetch` contract, the registry schema additions, and the dispatch
  registration.
- [quickstart.md](quickstart.md) — runnable validation scenarios.
- Agent context: the SPECKIT block in `AGENTS.md` (CLAUDE.md symlink) is
  repointed to this plan.

### Post-Design Constitution Re-Check

No change after design: the only dependency remains `beautifulsoup4`
(justified above); no infra, no schema change, no new env var, and the dedupe
reconciliation keeps `schema.py` untouched. Gate still **PASS**.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| New heavy dependency `beautifulsoup4` (Constitution IV) | First server-rendered HTML scrape; needs a resilient tree/selector parser so FR-005 (never falsely reject) and FR-014 (broken vs. empty) hold | stdlib `html.parser` is event-driven with no tree/selectors — robust extraction becomes brittle stateful callbacks (rejected in clarification); `lxml` adds a native `libxml2` binary footprint with deploy risk and no benefit for a daily single-board scrape |
