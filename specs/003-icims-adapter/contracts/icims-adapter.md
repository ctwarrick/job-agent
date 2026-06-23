# Contract: iCIMS adapter interface

The interfaces this feature exposes to the rest of the pipeline and to the
maintainer. All three already exist for other vendors; iCIMS conforms to them
rather than adding new surface.

## 1. Adapter function

```python
# src/job_agent/adapters/icims.py
def fetch(slug: str) -> list[Posting]: ...
```

- **Input**: `slug` — the token from a `registry.txt` `icims` line (the adapter
  splits its own compound slug internally).
- **Output**: a list of normalized `Posting` records (US-only, deduped by
  numeric job ID), at most `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` when that cap is
  set.
- **Errors**: a per-tenant network/parse failure is contained — the function
  reports the failure for that tenant without raising an exception that aborts
  the whole fetch stage (Constitution V). Invalid *configuration* fails loud.
- **Side effects**: HTTP GETs to the public iCIMS career site only; politeness
  sleep (~1 req / 2–3 s) between requests. No writes — persistence is the
  caller's job.

## 2. Registry dispatch

```python
# src/job_agent/fetch.py
ADAPTERS = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "workday": workday.fetch,
    "icims": icims.fetch,   # NEW
}
```

- Registry line: `icims  <tenant[:host]>` (single colon-delimited token).
- `fetch.load_registry` and the `fetch(slug) -> list[Posting]` contract are
  unchanged.

## 3. Configuration

- `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` (existing, reused): optional integer cap
  on postings returned per tenant per run. Unset → no artificial cap beyond what
  the source yields.
- `companies.toml` `[display_names]` (existing, git-ignored): optional
  `tenant -> "Display Name"` mapping; absent → company falls back to the tenant
  slug (never empty, since it feeds the fingerprint). A sample iCIMS entry is
  added to the committed `companies.toml.example`.

## Conformance tests (stub-based, no network)

The committed suite (`tests/test_icims.py`) asserts this contract against the
recon-captured fixtures: list/sitemap parse → `Posting`s; US filter; cap; stable
dedupe across runs; contained per-tenant failure; missing-description handling.
