# Implementation Plan: iCIMS ATS Adapter

**Branch**: `003-icims-adapter` | **Date**: 2026-06-22 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/003-icims-adapter/spec.md`

## Summary

Add `src/job_agent/adapters/icims.py` exposing `fetch(slug) -> list[Posting]`,
registered as `"icims"` in `fetch.py`'s `ADAPTERS` table, so iCIMS-hosted
employers (the confirmed tenants) flow into the fetch → score → digest
pipeline like Greenhouse/Lever/Workday. Unlike those vendors, the iCIMS public
career portal has **no authenticated-free JSON API** — access is via the public
career site (`{tenant}.icims.com`): `sitemap.xml` for discovery and
server-side-rendered HTML detail pages, with a possible per-tenant internal
JSON endpoint. The exact shape, and the `careers-{co}.icims.com` vs
`jobs.{co}.com` hosting question, genuinely varies per tenant, so the plan
**leads with a throwaway recon spike against the two confirmed tenants** that
captures real fixtures before any test or production constant is written —
applying the Workday retro lesson (verify a live external API before the gate;
build fakes from the real shape).

## Technical Context

**Language/Version**: Python ≥ 3.12 (`uv`).

**Primary Dependencies**: `requests` (HTTP, already used by greenhouse/lever/
workday). HTML/XML parsing defaults to **stdlib** (`xml.etree.ElementTree` for
`sitemap.xml`, `html.parser`/`re` for detail fields). A third-party parser
(`beautifulsoup4`) is adopted **only if** the recon spike proves robust
extraction needs it, and then only with a Complexity Tracking justification
(Constitution IV). Default: no new dependency.

**Storage**: existing SQLite `jobs.db` via `store.py`; no schema change.

**Testing**: `pytest`, stub-based, no network — matching `tests/`. Fixtures are
the real responses captured by the recon spike.

**Target Platform**: Linux; runs inside the existing scheduled pipeline.

**Project Type**: single-project CLI/pipeline; one adapter module per vendor.

**Performance Goals**: bounded by `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` and a
politeness sleep (~1 request / 2–3 s per the iCIMS scraping guidance) so a large
board neither floods the store nor trips bot detection.

**Constraints**: US-only postings filtered deterministically before scoring
(Constitution VII); per-tenant failure must not abort the run (V); personal
runtime files stay git-ignored (VI).

**Scale/Scope**: two confirmed tenants now; the verify-then-wire targets are
later and out of scope for this feature. The named company list lives only in
the git-ignored plans/adapter-implementation-sequence.md.

## Constitution Check

*GATE: re-checked after Phase 1 design — still passing.*

- **I. Cost Discipline**: no new cloud resource; iCIMS access is plain HTTPS
  from the existing job. LLM spend stays bounded by the reused per-employer cap
  plus US pre-filtering. Expected monthly cost impact: **$0 new infra**;
  marginal Anthropic cost proportional to the (capped) iCIMS posting volume.
  PASS.
- **II. Cloud-Native Scheduled Operation**: adapter is invoked by the existing
  `main.py` fetch stage; introduces no new manual production operation. PASS.
- **III. Test-First Delivery**: TDD with stub-based, no-network tests. The
  recon spike is a **dev-only, throwaway** network investigation that produces
  fixtures — it is never part of the committed suite. PASS.
- **IV. Simplicity & Stdlib-First**: one new adapter module, existing contract,
  stdlib parsing by default. Any new dependency requires a Complexity Tracking
  row (none required at plan time). PASS.
- **V. Fail Loud, Fail Visibly**: per-tenant errors degrade gracefully and stay
  visible; missing/invalid config fails hard before side effects. PASS.
- **VI. Personal-Data Privacy**: `registry.txt`, `companies.toml`, `jobs.db`
  remain git-ignored; only the adapter, tests, and an example template are
  committable. No personal data quoted. PASS.
- **VII. LLM Spend Efficiency**: deterministic US-location filtering runs before
  the LLM; the per-employer cap is the hard guardrail; existing prompt caching
  is untouched. PASS.

No violations → Complexity Tracking is empty.

## Project Structure

### Documentation (this feature)

```text
specs/003-icims-adapter/
├── plan.md              # This file
├── research.md          # Phase 0: recon findings + access-method decision
├── data-model.md        # Phase 1: Posting reuse + iCIMS registry entry
├── quickstart.md        # Phase 1: validation/run guide
├── contracts/
│   └── icims-adapter.md  # Phase 1: adapter + registry-line + env-var contract
└── checklists/
    └── requirements.md   # spec quality checklist (from /speckit-specify)
```

### Source Code (repository root)

```text
src/job_agent/
├── fetch.py                 # register "icims" in ADAPTERS (1 line)
└── adapters/
    ├── greenhouse.py        # existing pattern reference (JSON)
    ├── lever.py             # existing pattern reference (JSON)
    ├── workday.py           # existing pattern reference (compound slug, 2 hops)
    └── icims.py             # NEW: fetch(slug) -> list[Posting]

tests/
├── test_workday.py          # pattern reference for stub-based adapter tests
└── test_icims.py            # NEW: list parse, US filter, cap, dedupe, failure

companies.toml.example       # extend with an iCIMS tenant -> display-name sample
```

**Structure Decision**: single-project layout, reusing the established
`adapters/<vendor>.py` + `ADAPTERS` registration pattern. `icims.py` mirrors
`workday.py` (compound slug, optional second round-trip, politeness sleep, cap)
rather than inventing new structure — directly applying the implementer retro
note "match the nearest sibling's shape."

## Phase 0 — Recon spike (research.md)

Leads the build. Throwaway, network-allowed, not committed. Against the two
confirmed tenants (named in the git-ignored adapter-sequence plan):

1. **Confirm it is iCIMS** via the detection hook (career-site HTML / redirects
   contain `icims.com`); resolve the real host (`{tenant}.icims.com`,
   `careers-{tenant}.icims.com`, or a `jobs.{co}.com` custom domain).
2. **Pick the access method**: (a) `sitemap.xml` discovery + HTML detail pages,
   (b) `/jobs/search?pr={page}&in_iframe=1` paginated HTML, or (c) an internal
   JSON endpoint if one exists for that tenant. Record which works per tenant.
3. **Capture real fixtures**: one listing/sitemap response and one job-detail
   response per access method, saved as test fixtures.
4. **Confirm field availability**: numeric job ID (dedupe key, regex
   `/jobs/(\d+)/`), title, location (for US filtering), description, canonical
   URL, posted date if present.

Findings + the chosen method and any tenant-specific constant land in
`research.md` (Decision / Rationale / Alternatives), and the captured fixtures
seed the tests. **No production constant is hardcoded before this runs.**

## Phase 1 — Design & contracts

- **data-model.md**: reuse `schema.Posting` + fingerprint unchanged; document
  the iCIMS registry-entry shape and the recon-confirmed source fields.
- **contracts/icims-adapter.md**: the `fetch(slug) -> list[Posting]` contract,
  the `icims` registry-line format, the reused `JOBAGENT_MAX_POSTINGS_PER_
  EMPLOYER` env var, and the `companies.toml` display-name fallback.
- **quickstart.md**: run the recon spike → run `pytest` → wire one tenant
  locally and run a capped `jobagent-fetch` to confirm upsert.

## Implementation steps (ordered; TDD-gated, human-approved before build)

1. **Recon spike** (Phase 0) — confirm shape, capture fixtures. Gate: real
   fixtures exist for both confirmed tenants before proceeding.
2. **test-writer (red)**: `tests/test_icims.py` from the captured fixtures —
   list/sitemap parsing → `Posting`s; US-location filter keeps US, drops
   non-US, and retains unparseable/ambiguous-location postings;
   `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` caps results; dedupe fingerprint stable
   across runs; a single-tenant fetch error returns empty/raises-caught without
   aborting; missing description handled. Shown red.
3. **implementer (green)**: `icims.py` mirroring `workday.py`; register
   `"icims"` in `fetch.py` `ADAPTERS`; stdlib parsing unless the spike proved
   otherwise; uniform `resp.raise_for_status()` + parse (no per-call
   duck-typing — Workday retro). Extend `companies.toml.example`.
4. **reviewer**: fresh-context diff + plan; re-runs `uv run pytest`. Confirms
   the spike-verified shape matches the committed fixtures and that no
   unverified constant slipped in. No infra touched → `validate-infra.sh` N/A.
5. **Local wiring (git-ignored, post-green)**: add the confirmed tenants'
   `icims` lines to `registry.txt` and display names to `companies.toml`; run a
   capped `jobagent-fetch` to confirm upsert. Not committed.
6. **Human commit gate**, then releaser (1.2.0) → retrospective.

## Risks / open questions

- **Endpoint shape is genuinely uncertain and per-tenant** (HTML vs internal
  JSON; `careers-*.icims.com` vs `jobs.{co}.com`). Mitigation: the recon spike
  is step 1 and gates everything; fixtures come from real captures, not guesses
  — verified before the commit gate (Workday retro lesson). A tenant served
  from a `jobs.{co}.com` custom domain in particular may be a non-iCIMS front
  and must be confirmed as iCIMS, not assumed.
- **HTML parsing may pull in a dependency.** If the spike shows stdlib
  extraction is too brittle, adding `beautifulsoup4` needs a Complexity
  Tracking row and human sign-off at the gate; default remains stdlib.
- **Coupled artifacts**: `icims.py` ⇄ `fetch.py` `ADAPTERS` (registration),
  and `icims.py` ⇄ the captured fixtures in `tests/` (the fixture shape is the
  contract the parser must satisfy) — re-validate together if either changes.
- **Bot detection / rate limits**: honor ~1 req / 2–3 s; the per-employer cap
  bounds total requests per run.

## Complexity Tracking

> No Constitution Check violations — table intentionally empty.
