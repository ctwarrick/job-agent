# Implementation Plan: Structured TOML source registry

**Branch**: `004-registry-toml-config` | **Date**: 2026-06-23
**Spec**: [spec.md](./spec.md)

## Summary

Replace the flat `registry.txt` with a validated TOML registry
(`registry.toml`, `[[source]]` array-of-tables) read by a new
`src/job_agent/registry.py` module — mirroring `filter.py` (config file → typed
dataclass → fail-loud loader). **`registry.toml` becomes the single source of
truth for each board: which to poll AND its display company.** The loader
resolves the final company name and the per-vendor slug; `fetch.main()` passes
the company to the adapter; `companies.toml` and the duplicated
`_resolve_company` helpers are **retired**. Ship a committed `.example`; create
the real, git-ignored `registry.toml` populated with all 14 current sources for
upload today.

**Cost impact (Principle I):** $0 — no new cloud resources, no LLM calls,
stdlib-only (`tomllib`). No new dependency. Net code is *smaller* (one config
file and two helper copies deleted).

## Technical Context

- **Language/Version**: Python ≥ 3.12 (`tomllib` is stdlib).
- **Primary Dependencies**: none added; stdlib `tomllib`, `dataclasses`.
- **Storage**: runtime file on the Azure Files share (like `registry.txt`); one
  file now (`companies.toml` removed from the share at cutover).
- **Testing**: `pytest`, stub-based, no network.
- **Project Type**: single-project CLI pipeline.
- **Constraints**: adapter contract gains a `company` kwarg (minimal change);
  the packed-slug reconstruction (`_split_slug`) is **unchanged** this feature;
  no personal data in committed files; code releases and registry edits stay
  decoupled.

## Constitution Check

*GATE — re-checked after design below.*

| Principle | Verdict | Note |
|---|---|---|
| I. Cost Discipline | ✅ | $0; no resources, no LLM. |
| II. Cloud-Native / runtime-file exception | ✅ | Extends the sanctioned manual runtime-file update; **fewer** files to sync (registry.toml only). Constitution text names `registry.txt`; refresh later, not blocking. |
| III. Test-First | ✅ | TDD list below; reviewer re-runs `pytest`. |
| IV. Simplicity / Stdlib-First | ✅ (improvement) | `tomllib` stdlib; **net simplification** — deletes `companies.toml`, its `.example`, two `_resolve_company` copies, and two `tomllib` imports. New `registry.py` mirrors `filter.py`. |
| V. Fail Loud | ✅ (improvement) | Hard validation raises before any external effect, replacing soft skip-and-continue. |
| VI. Personal-Data Privacy | ✅ | `registry.toml` git-ignored; artifacts quote no real companies; `.example` uses public slugs. One fewer personal file on the share. |
| VII. LLM Spend Efficiency | ✅ (N/A) | No LLM calls in this path. |

No violations → Complexity Tracking empty.

## Project Structure

```text
src/job_agent/
├── registry.py      # NEW: Source dataclass + load_registry (validated, resolves company)
├── fetch.py         # CHANGED: import load_registry; loop now `for source in ...`, passes company
└── adapters/
    ├── workday.py   # CHANGED: drop _resolve_company + tomllib; take `company` kwarg
    └── icims.py     # CHANGED: same
    # greenhouse.py / lever.py: CHANGED — accept `company` kwarg (default = slug)

tests/
├── test_registry.py # NEW: parse/validate/slug + company-resolution tests
├── test_fetch.py    # CHANGED: failure-record tests build Source objects; drop 4 .txt tests
├── test_workday.py  # CHANGED: replace 2 companies.toml tests with company-from-arg
└── test_icims.py    # CHANGED: same

# repo root
registry.toml.example # NEW, committed
registry.toml         # NEW, git-ignored, populated (upload-today deliverable)
companies.toml.example # DELETED (committed file removed)
.gitignore            # CHANGED: + registry.toml; - companies.toml (at file deletion)
```

**Structure Decision**: dedicated `registry.py` parallels `filter.py` (config →
typed record → validating loader) and becomes the home for company resolution,
so the two adapter `_resolve_company` copies collapse into one place.

## Design

### Loader returns resolved `Source` objects

`load_registry(path: str | None = None) -> list[Source]`

`Source` dataclass: `vendor: str`, `slug: str` (reconstructed), `company: str`
(resolved).

1. `store.data_path("registry.toml")`; `tomllib.load`.
2. Validate each `[[source]]` (see data-model); skip `enabled = false`.
3. Reconstruct slug: gh/lever → `slug`; workday → `tenant:site:host`; icims →
   `tenant` or `tenant:host`.
4. **Resolve company**: `name` if present, else the vendor default —
   `slug` for greenhouse/lever, `tenant` for workday/icims.
5. Duplicate `(vendor, slug)` → raise.

### `fetch.main()` passes company

```python
for source in load_registry():
    fn = ADAPTERS.get(source.vendor)
    ...
    postings = fn(source.slug, company=source.company)
    failures.append({"source": source.vendor, "company_slug": source.slug, ...})
```

### Adapter contract: add a `company` kwarg

`fetch(slug: str, *, company: str | None = None, timeout: int = 20)`
- greenhouse/lever: `company = company or slug` (unchanged effective value).
- workday/icims: `company = company or tenant`; **delete `_resolve_company`,
  the `companies.toml` read, and `import tomllib`**; update module docstrings.

Resolution lives canonically in the loader; the adapter `or` keeps each adapter
runnable standalone in tests/debug.

### Retire `companies.toml`

Delete `companies.toml.example` (committed), the runtime `companies.toml`
(git-ignored), the `.gitignore` line, and the two `_resolve_company` helpers and
their tests. `registry.toml`'s `name` is the only company source.

### Fingerprint stability — now LIVE and critical (FR-005)

Because `name` is authoritative *this release*, the resolved company must equal
today's effective company or dedupe re-surfaces every posting. The populated
`registry.toml` already satisfies this: workday `name`s equal the old
`companies.toml` display strings exactly; greenhouse/lever omit `name` so
company falls back to the slug. **Verify before upload** (quickstart §2).

### TDD test list (author red first)

`tests/test_registry.py` (new):
1. loads each vendor, reconstructs slug, resolves company (name → vendor default).
2. `enabled = false` omitted.
3. raises on unknown vendor.
4. raises on missing required field (e.g. workday `host`).
5. raises on duplicate `(vendor, slug)`.
6. raises on unknown key (typo guard).
7. `JOBAGENT_DATA_DIR` honored.
8. inline `#` comments ignored.

`tests/test_fetch.py`: drop the 4 `.txt` tests; update failure-record tests to
build `Source` objects and assert `company` passed to the adapter.
`tests/test_workday.py` / `tests/test_icims.py`: replace the 2 companies.toml
resolution tests each with "company comes from the `company` arg; defaults to
tenant when absent."

### Steps

1. **Red**: write `tests/test_registry.py`; adjust `test_fetch.py` /
   `test_workday.py` / `test_icims.py` to the new contract; prove red.
2. **Green**: add `registry.py`; switch `fetch.py` loop + import; add `company`
   kwarg to all 4 adapters; delete `_resolve_company`/`tomllib` from
   workday+icims; delete `companies.toml.example`; update docstrings.
3. Add `registry.toml.example`; create git-ignored `registry.toml` (done);
   `.gitignore` += `registry.toml`, − `companies.toml`.
4. `uv run pytest` green; reviewer re-runs.

## Risks & operational notes

- **Fingerprint drift (now live):** a wrong/changed `name` re-surfaces a
  company's whole backlog. Mitigated by matching current companies.toml values;
  must be verified.
- **Cutover (one-time):** upload `registry.toml` **first**, then deploy
  (loader fails loud if absent), then delete `registry.txt` **and**
  `companies.toml` from the share in the same session. After that, registry
  edits and code releases are decoupled.
- **Does not fix drift:** uploading fixes today's missing workday sources; the
  durable anti-drift fix (coverage/parity check) is a separate workstream.

## Future work (out of scope here)

- Adapter contract → structured `Source` config; delete `_split_slug` packing.
- Wire `max_per_employer` per-source (today the global env var still governs).
- Registry coverage/parity check to catch deployment drift.
