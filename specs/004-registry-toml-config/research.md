# Research: Structured TOML source registry

## Decision 1 — Format: TOML array-of-tables

**Decision**: `registry.toml` with one `[[source]]` table per board.

**Rationale**: The repo already standardized on TOML + stdlib `tomllib`
(`filter.toml`, `companies.toml`); `registry.txt` is the lone bespoke dialect.
TOML keeps `#` comments (the slug-trap notes carry real tribal knowledge), maps
cleanly to per-vendor fields, and adds no dependency (Principle IV).

**Alternatives considered**:
- *Keep `.txt` + add validation* — lowest churn, but doubles down on the
  colon-packed slug for workday/icims and leaves two config dialects. Rejected.
- *JSON* — stdlib but **no comments** (loses slug-trap notes) and edit-hostile.
  Rejected.
- *YAML* — pleasant nesting but a **non-stdlib dependency** for a file `tomllib`
  already handles. Violates stdlib-first. Rejected.

## Decision 2 — Conservative loader: return `list[(vendor, slug)]`

**Decision**: The loader validates structured TOML but reconstructs and returns
the same `(vendor, slug)` tuples `fetch.main()` consumes today.

**Rationale**: Keeps the `fetch(slug: str)` adapter contract and all adapter
modules/tests untouched, shrinking blast radius to one new module + one import
line. The contract evolution (drop `_split_slug`) is a clean, separable follow-up.

**Alternatives**: return rich `Source` objects and thread company/options into
adapters now — larger diff touching every adapter + test; deferred.

## Decision 3 — New module `registry.py` vs. inline in `fetch.py`

**Decision**: New `src/job_agent/registry.py` (`Source` dataclass +
`load_registry`).

**Rationale**: Direct parallel to `filter.py` — a runtime config file deserves a
typed record and a validating loader in its own module; keeps `fetch.py` lean
and gives validation a clear test home (`tests/test_registry.py`).

**Alternative**: keep it in `fetch.py`. Workable, but `fetch.py` is the
orchestrator, and the validation logic grows enough to warrant separation.
`fetch.py` re-exports via `from .registry import load_registry` so existing
monkeypatch-based tests keep working.

## Decision 4 — Fail-loud validation surface

**Decision**: Raise on unknown vendor, missing required field, duplicate
`(vendor, slug)`, and unrecognized per-source keys (typo guard). Parse errors
surface as `tomllib.TOMLDecodeError`; schema errors as `ValueError` naming the
offending source.

**Rationale**: Principle V — configuration errors must fail hard before any
external effect. The old loader's soft "log and skip" is the silent-degradation
pattern behind the recent incident. Mirrors `filter.py`'s `load_criteria`.

## Decision 5 — `name` authoritative now; retire `companies.toml`

**Decision** (revised per maintainer): wire `name` through this release. The
loader resolves company, `fetch.main()` passes it to the adapter via a new
`company` kwarg, and `companies.toml` + the two `_resolve_company` helpers are
deleted. One source of truth, one file to sync.

**Rationale**: keeping company data in two files was the smell; consolidating now
removes a whole config file, two duplicated helpers, and two `tomllib` imports —
a net simplification (Principle IV). The cost is a slightly larger diff (4
adapters + their tests) and a **live** fingerprint-stability constraint
(data-model FR-005), accepted because the populated file already matches current
values.

**Trade**: this minimally extends the adapter contract (`company` kwarg). The
*full* contract change (structured `Source`, dropping `_split_slug`) remains
deferred — slug reconstruction/splitting is unchanged here.

## Decision 6 — Cutover sequencing

**Decision**: Upload `registry.toml` to the share **before** deploying the
TOML-reading code; loader fails loud if the file is absent.

**Rationale**: A one-time ordering constraint at the format switch. After it,
registry edits and code releases are decoupled (the stated goal). A fail-loud
absent-file error is visible (Principle V), not a silent empty run.
