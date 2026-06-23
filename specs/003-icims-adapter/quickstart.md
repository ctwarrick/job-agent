# Quickstart: validating the iCIMS adapter

Validation/run guide for the iCIMS adapter. Implementation detail lives in the
plan and (after `/speckit-tasks`) in `tasks.md`; this file is how you prove the
feature works end to end.

## Prerequisites

- `uv sync`
- A confirmed iCIMS tenant resolved by the recon spike (see the git-ignored
  plans/adapter-implementation-sequence.md for the tenant list).
- Optional: `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` set to a small number while
  validating against large boards.

## 1. Recon spike (dev-only, network; not committed)

Confirm the live shape before trusting any fixture:

- Verify the tenant is iCIMS (detection hook: career-site HTML / redirects
  contain `icims.com`); resolve the real `{tenant}.icims.com` host.
- Pull `https://{tenant}.icims.com/sitemap.xml` and one
  `.../jobs/{id}/{slug}/job` detail page; confirm title, location, description,
  numeric ID, and any posted date are present.
- Save those responses as fixtures under `tests/` for the test-writer.

Expected: real fixtures captured for the confirmed tenant(s); access method
recorded in [research.md](research.md).

## 2. Unit suite (stub-based, no network)

```bash
uv run pytest -q
uv run pytest -q tests/test_icims.py
```

Expected: green. `test_icims.py` covers list/sitemap parsing → `Posting`,
US-only filtering, the per-employer cap, stable dedupe across two runs, contained
single-tenant failure, and missing-description handling — all against the
captured fixtures, no network.

## 3. Local end-to-end fetch (git-ignored wiring)

```bash
# add to the git-ignored registry.txt:
#   icims  <tenant>
# optional display name in companies.toml [display_names]
JOBAGENT_MAX_POSTINGS_PER_EMPLOYER=10 uv run jobagent-fetch
```

Expected: the tenant's open US postings upsert without error, capped at 10,
visible to the score stage. Re-running does not create duplicates (dedupe).

## 4. Pipeline smoke (optional)

```bash
JOBAGENT_MAX_POSTINGS_PER_EMPLOYER=10 uv run python main.py
```

Expected: the digest includes iCIMS-sourced postings alongside
Greenhouse/Lever/Workday; a failing tenant degrades gracefully and the digest
still sends.

## References

- Contract: [contracts/icims-adapter.md](contracts/icims-adapter.md)
- Field mapping: [data-model.md](data-model.md)
- Access-method decision: [research.md](research.md)
