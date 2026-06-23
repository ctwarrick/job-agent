# Feature Specification: Structured TOML source registry

**Branch**: `004-registry-toml-config` | **Date**: 2026-06-23
**Status**: Draft (planning)

## Why

The source registry is a flat `registry.txt` of `vendor slug` lines. Two
adapters (workday, icims) already cram structured data into the single "slug"
token via colon-packing (`tenant:site:host`, `tenant:host`), each with a
hand-rolled `_split_slug` and its own malformed-slug error path. Display names
live in a *separate* `companies.toml`, joined back by the tenant string. The
flat format has outgrown its shape, and its parser fails *soft* (logs a bad
line, continues) — the same silent-degradation pattern that recently let an
entire vendor's sources go missing in production with no error.

## What

Replace `registry.txt` with a structured TOML file (`registry.toml`,
array-of-tables `[[source]]`), loaded with stdlib `tomllib` and **validated
fail-loud** like `filter.py`'s `load_criteria` (unknown vendor, missing
required field, duplicate source → raise before any fetch).

### In scope

- New `registry.toml` schema: per-vendor fields instead of a packed slug, an
  optional display `name`, an `enabled` flag, and a reserved slot for
  per-source options (e.g. `max_per_employer`).
- A validated loader returning resolved `Source` records (vendor, reconstructed
  slug, and the final display company). `registry.toml` is the **single source
  of truth** for both which boards to poll and their company names.
- **`name` wired authoritative now**: `fetch.main()` passes the company to the
  adapter, which gains a `company` kwarg; `companies.toml` and the duplicated
  `_resolve_company` helpers are **retired**.
- A committed `registry.toml.example` (schema doc), following the existing
  `filter.toml.example` pattern.
- A populated, git-ignored `registry.toml` containing every currently targeted
  source (2 greenhouse + 2 lever + 10 workday = 14), ready to upload to the
  prod Azure Files share today.

### Out of scope (explicit future work)

- Evolving the adapter contract to a full structured `Source` object and
  deleting the `_split_slug` packing (this feature only adds a `company` kwarg;
  the packed slug is still reconstructed and split internally).
- Wiring `max_per_employer` per-source (today it is the global
  `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` env var). The schema reserves the field;
  behavior is unchanged.
- Fixing production deployment drift (the root cause of the recent incident).
  That is a complementary workstream — a coverage/parity check plus a deploy
  step — tracked separately, not delivered here.

## Functional requirements

- **FR-001** The loader MUST read `registry.toml` via `tomllib` from
  `store.data_path("registry.toml")`, honoring `JOBAGENT_DATA_DIR`.
- **FR-002** The loader MUST return resolved `Source` records (vendor,
  reconstructed slug, resolved company); `fetch.main()` MUST pass `company` to
  the adapter, which gains a `company` kwarg. Effective slug dispatch is
  unchanged.
- **FR-003** The loader MUST raise (fail loud, Principle V) on: unknown
  `vendor`; a missing required field for the vendor; a duplicate
  `(vendor, slug)`; an unrecognized key on a source (catches typos).
- **FR-004** A source with `enabled = false` MUST be omitted from the returned
  list (disabled without deletion, so it stays present for future drift
  checks).
- **FR-005** `name` is authoritative for the digest company this release. The
  resolved company (`name`, else slug for gh/lever / tenant for workday/icims)
  MUST equal each source's current effective company, or dedupe fingerprints
  shift and re-surface postings (see data-model "Fingerprint-stability rule").
- **FR-007** `companies.toml`, its committed `.example`, and the two
  `_resolve_company` helpers MUST be removed; `registry.toml` is the sole
  company source.
- **FR-006** The populated `registry.toml` MUST be git-ignored and MUST NOT be
  committed; its contents MUST NOT be quoted in any committed artifact
  (Principle VI). The committed `.example` MUST use only illustrative,
  non-personal slugs already public in the repo.

## Success criteria

- `uv run pytest` green, including new loader tests authored red-first.
- Loading the populated `registry.toml` returns 14 `(vendor, slug)` tuples.
- The user can upload `registry.toml` to the share today and the next prod run
  fetches all four vendors.

## Review checklist

- [ ] No personal data (real company list) in any committed file.
- [ ] Adapter modules and `fetch.main()` unchanged in this feature.
- [ ] Loader fails loud on malformed config; no silent skips.
