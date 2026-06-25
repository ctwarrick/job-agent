# Quickstart: Talemetry / TTC-Portals ATS Adapter

Runnable validation that the adapter works end-to-end. Field shapes and the
behavioral contract live in [data-model.md](data-model.md) and
[contracts/talemetry-adapter.md](contracts/talemetry-adapter.md); this guide is
the run/validation steps, not implementation.

## Prerequisites

```bash
uv sync          # installs deps, including the new beautifulsoup4
```

## 1. Test suite (no network) — primary gate

The whole feature is provable offline with stub-based tests (FR-011); this is
the authoritative validation.

```bash
uv run pytest -q                      # full suite stays green (SC-005)
uv run pytest tests/test_talemetry.py # the adapter's own stub tests
uv run pytest tests/test_registry.py  # talemetry validation/slug/company
```

Expected: green. The adapter tests exercise listing parse, detail/description
handling (FR-013), keep-if-any US filtering (FR-005), the per-employer cap
(FR-006), `external_id` stability / re-crawl dedupe (SC-004), source-failure
propagation (FR-007), the zero-posting warning (FR-014/SC-007), and the
unparseable-ID skip.

## 2. Privacy grep — committed-artifact check

```bash
git diff --staged | grep -i "<real-employer-name>"   # must return nothing
grep -rn "careers.example.com" registry.toml.example  # placeholder present
```

Expected: no real employer name anywhere in the diff (SC-006); only the
platform name and the placeholder host appear (FR-012).

## 3. Live wiring — local, after the suite is green

The real host is added to the git-ignored `registry.toml` only (never the
committed example). This is the single local step outside the committable diff.

```toml
# registry.toml  (git-ignored)
[[source]]
vendor = "talemetry"
name   = "Acme Regional"        # authoritative digest company
host   = "careers.<real-host>"  # the real Talemetry-fronted host
```

```bash
uv run jobagent-fetch           # fetch stage only
```

Expected (SC-001): the employer's open US postings are upserted as normalized
postings and become scoreable alongside the Greenhouse/Lever/Workday/iCIMS
sources, with no per-posting handling.

## 4. Cap and resilience spot-checks (optional, live)

```bash
JOBAGENT_MAX_POSTINGS_PER_EMPLOYER=5 uv run jobagent-fetch   # SC-002
```

Expected: no more than 5 postings from the Talemetry source, and paging stops
at the cap. With the host temporarily set to an unreachable value, the run
still delivers the digest from the other sources and the failure is visible in
the run output (SC-003). A run that parses zero postings prints a distinct
warning, not a failure (SC-007).

## 5. Re-crawl dedupe

```bash
uv run jobagent-fetch && uv run jobagent-fetch   # run twice
```

Expected (SC-004): a posting seen on both runs produces exactly one stored row
— the content fingerprint collapses the duplicate.
