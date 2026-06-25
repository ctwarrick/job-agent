# Phase 0 Research: Talemetry / TTC-Portals ATS Adapter

All NEEDS CLARIFICATION items from Technical Context are resolved below. The
one item deliberately left as a recon spike — the exact scraped-HTML structure
— follows the Workday/iCIMS "verify live before the commit gate" discipline:
selectors and the non-US marker set are placeholders in code, confirmed against
the live target host before the diff is committed, never invented blind.

## Decision 1 — HTML parser dependency

**Decision**: Add `beautifulsoup4>=4.12`, used with its built-in `html.parser`
backend. No `lxml`.

**Rationale**: This is the project's first server-rendered HTML scrape; the
existing four adapters parse JSON the stdlib handles, so no precedent exists.
`bs4` on `html.parser` is pure-Python (transitive dep `soupsieve` is too) with
no C extension or system library, so it builds and deploys cleanly into the
consumption-based Azure compute (Principle II) with no binary-wheel risk. Its
tree/selector model degrades to a visible empty parse rather than a silent
miss, which is what FR-005 (never falsely reject) and FR-014 (broken vs. empty)
require.

**Alternatives considered**:
- *stdlib `html.parser` only* — rejected in the 2026-06-24 clarification
  session: an event-driven `HTMLParser` subclass has no tree navigation or
  selectors, so robust extraction becomes brittle stateful tag callbacks that
  re-break on cosmetic markup changes.
- *`lxml`* — faster, but pulls native `libxml2`/`libxslt` bindings, a heavier
  and more fragile deploy footprint; speed is irrelevant for a once-daily
  single-board scrape.
- *`selectolax`* — also a C extension; same deploy objection, smaller
  ecosystem.

## Decision 2 — Listing, pagination, and detail recon plan

**Decision**: Treat the listing page as the source of job-entry anchors
(`/jobs/{id}-{slug}/`); page through it until no further entries parse; fetch
each job's detail page only when the listing entry lacks the description text
the scorer needs (FR-013), mirroring Workday's two-round-trip shape.

**Rationale**: The numeric ID + slug URL pattern is the one structural fact the
spec asserts as stable (Assumptions); anchoring on it keeps the
`external_id`/URL derivation deterministic. Detail fetches are gated by need
(only when the listing omits the description) and bounded by the per-employer
cap (FR-006) and politeness sleep (FR-008), so a large board cannot fan out
into unbounded round-trips.

**Recon spike (confirm live before commit)**: the concrete CSS selectors for
(a) the job-entry anchors on the listing, (b) the pagination mechanism
(next-link vs. `?page=N` vs. offset), (c) where the location string lives, and
(d) where the description lives on the detail page. These land as named module
constants/helpers and are verified against the live host before the commit
gate — exactly as Workday's `USA_COUNTRY_FACET_KEY`/`USA_COUNTRY_WID` were.

**Alternatives considered**: always fetching the detail page (rejected —
wastes round-trips when the listing already carries the description);
never fetching it (rejected — leaves the scorer with empty descriptions,
the iCIMS "exclude empty description" path, which here would silently drop
otherwise-valid postings).

## Decision 3 — US scoping on free-text location

**Decision**: A deterministic, keep-if-any `_is_us(location: str) -> bool`
gate: return `False` only when the location string contains a marker
positively identified as non-US (a small, documented set of country/territory
tokens); otherwise return `True`, retaining US and ambiguous/empty locations
for scoring.

**Rationale**: A scraped HTML board has no server-side US facet (unlike
Workday) and no structured `country_code` (unlike iCIMS), so the gate operates
on the free-text location. The keep-if-any philosophy (FR-005, Constitution
VII "filter before you spend, but never falsely reject") means over-include and
let LLM scoring resolve the bleed, rather than risk dropping a US posting whose
location string is unusual. The marker set is small and confirmed during recon;
it is tuning, not business logic, consistent with Principle IV.

**Alternatives considered**: a US-state allowlist (rejected — inverts the
keep-if-any default and would drop US postings phrased as "Remote (US)" or
bare city names); no filter at all (rejected — Constitution VII requires the
deterministic pre-filter so obviously-non-US rows never reach the LLM).

## Decision 4 — Dedupe identity (external_id vs. fingerprint)

**Decision**: The numeric job ID is the posting's `external_id`; cross-run
dedupe is delivered by the existing content-based `schema.Posting.fingerprint`,
which is **not** modified for this adapter.

**Rationale**: `store.upsert_postings` dedupes on the `fingerprint` PRIMARY KEY
(`INSERT OR IGNORE`), and that fingerprint is `title|company|location|
description` by deliberate cross-source design (`_migrate_fingerprints` rebuilds
rows with `external_id=""`). The numeric-ID-keyed detail fetch makes stored
content deterministic, so the content fingerprint is stable across re-crawls —
satisfying SC-004 without inventing a per-adapter fingerprint that would
contradict `schema.py`. See plan.md "dedupe identity reconciliation."

**Alternatives considered**: an `external_id`-based fingerprint for this
adapter (rejected — diverges from the shared, intentional dedupe model and
breaks cross-source collapse).

## Decision 5 — Zero-posting and unparseable-ID warnings

**Decision**: Emit distinct, visible `stderr` warning lines (matching
`fetch.py`'s `print(..., file=sys.stderr)` style) for two cases: (a) a fetch
that parses zero postings returns `[]` after warning (FR-014, SC-007), treated
as success-but-suspicious, not a raised error; (b) a listing entry whose URL
has no parseable numeric ID is skipped with its own warning and the run
continues.

**Rationale**: Reuses the established stderr logging channel, so the warnings
surface in the same place the maintainer already reads run output, and keeps a
broken scrape distinguishable from a genuinely empty board without false-
alarming. A hard failure is reserved for transport errors (HTTP), which
propagate to `fetch.main`'s per-source guard (FR-007).

**Alternatives considered**: Python `warnings`/`logging` module (rejected —
introduces a second logging idiom inconsistent with the existing adapters and
`fetch.py`); raising on zero postings (rejected — would false-alarm a real
empty board and abort that source needlessly).
