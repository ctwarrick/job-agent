# Phase 1 Data Model: iCIMS adapter

No new persisted entity. The adapter produces the existing `Posting`; storage
and dedupe are unchanged. This file records the field mapping and the registry
encoding the adapter parses.

## Posting (existing — `schema.py`)

Reused as-is. The adapter MUST populate the fields scoring and the dedupe
fingerprint depend on. Mapping from iCIMS source fields (confirmed by the recon
spike):

| Posting field | iCIMS source | Notes |
|---|---|---|
| `company` | `companies.toml` display name, else tenant slug | Never empty; feeds fingerprint. |
| `title` | job-detail title | |
| `location` | job-detail location text | Drives deterministic US filtering before scoring. |
| `description` | job-detail body | Scorer needs it; fetch detail if listing omits. |
| `url` | `https://{tenant}.icims.com/jobs/{id}/{slug}/job` | Canonical posting URL. |
| `posted_at` / date | detail or sitemap `lastmod` | Best-effort; optional if absent. |
| fingerprint input | numeric job `{id}` | Stable, deterministic dedupe key. |

Validation / rules:

- Non-US locations are dropped before scoring (Constitution VII).
- A posting whose description cannot be obtained is excluded rather than scored
  empty (the scorer needs description text).
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
