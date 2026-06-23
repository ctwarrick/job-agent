# Phase 1 Data Model: iCIMS adapter

No new persisted entity. The adapter produces the existing `Posting`; storage
and dedupe are unchanged. This file records the field mapping and the registry
encoding the adapter parses.

## Posting (existing — `schema.py`)

Reused as-is. The adapter MUST populate the fields scoring and the dedupe
fingerprint depend on. Mapping from iCIMS source fields (confirmed by the recon
spike):

Source fields are the Jibe `/api/jobs` JSON confirmed by the recon spike (see
[research.md](research.md)); each job is read from `jobs[].data`.

| Posting field | iCIMS (Jibe JSON) source | Notes |
|---|---|---|
| `company` | `companies.toml` display name, else tenant slug | Never empty; feeds fingerprint. |
| `title` | `data.title` | |
| `location` | `data.full_location` (else `data.location_name`) | Display text; US filtering keys off `country_code`, not this string. |
| `description` | `data.description` | Full HTML, **inline** — no detail round-trip. |
| `url` | `data.apply_url` | Canonical posting URL. |
| `posted_at` / date | `data.posted_date` | ISO-8601; best-effort if absent. |
| fingerprint input | `data.req_id` (numeric job ID) | Stable, deterministic dedupe key. |
| US filter input | `data.country_code` (ISO-2) | Keep `US`; drop positively non-US; retain empty/missing. |

Validation / rules:

- US filtering is deterministic on `country_code`: keep `US`, drop a code
  positively identified as non-US, retain an empty/missing code (FR-004,
  keep-if-any). Runs before scoring (Constitution VII).
- A posting with no description text is excluded rather than scored empty (the
  scorer needs description text).
- Result count per tenant per run is capped by
  `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER`.

## iCIMS registry entry (git-ignored `registry.txt`)

One line per tenant under the `icims` source keyword, dispatched through the
existing `fetch.py` `ADAPTERS` table. The adapter splits its own slug, so
`fetch.load_registry` and the `fetch(slug) -> list[Posting]` contract are
unchanged.

- Form: `icims  <slug>` where `<slug>` encodes the tenant (and host variant if
  needed) as a single colon-delimited token, mirroring the Workday convention
  (`tenant[:host]`). The exact token set is fixed by the recon spike once the
  per-tenant host/access method is confirmed.
- Display name: optional `[display_names]` entry in `companies.toml` mapping the
  tenant slug to a human-readable company; falls back to the slug when absent.

## State transitions

None. The adapter is a pure read: `slug -> list[Posting]`. Persistence,
upsert, and retention are owned by `store.py` and unchanged.
