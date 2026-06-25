# Phase 1 Data Model: Talemetry / TTC-Portals ATS Adapter

No new persisted entity and no schema change. The feature reuses the shared
`Posting` and adds one registry source variant.

## Posting (existing — `schema.py`)

The normalized job posting the whole pipeline shares; the Talemetry adapter
emits it via `schema.normalize(...)` exactly like the other adapters. Field
mapping for this adapter:

| Field | Source on the scraped page | Notes |
|---|---|---|
| `source` | constant `"talemetry"` | the vendor key |
| `company` | `company` kwarg → falls back to `host` | resolved upstream in `registry.py`; never empty (feeds fingerprint) |
| `external_id` | numeric ID from `/jobs/{id}-{slug}/` | FR-004 stable id; basis for the canonical URL and the skip-if-unparseable rule |
| `title` | listing/detail title | |
| `location` | listing/detail location string | passed through the keep-if-any US gate before inclusion |
| `description` | listing if present, else detail page | FR-013: fetch detail when listing omits it |
| `url` | `https://{host}/jobs/{id}-{slug}/` | canonical, ID-anchored |
| `posted_at` | listing/detail posting date if exposed | `None` when the source does not expose one |

**Dedupe identity**: `Posting.fingerprint` is unchanged —
`title|company|location|description`, hashed in `schema.py`. `external_id` is
stored but is **not** part of the fingerprint (see plan.md "dedupe identity
reconciliation" and research Decision 4). Cross-run stability (SC-004) comes
from the content fingerprint being deterministic once the numeric-ID-keyed
detail fetch fixes the content.

**Validation/derivation rules**:
- A listing entry with no parseable numeric ID is **skipped** with a visible
  warning (the ID is required to build a stable URL/`external_id`).
- `location` is retained unless positively identified as non-US (`_is_us`
  keep-if-any, FR-005).
- `company` defaults to `host` when the `company` kwarg is absent (FR-009), so
  the fingerprint component is never empty.

## Talemetry registry source (new variant — `registry.toml`)

One `[[source]]` table identifying a single Talemetry-hosted careers site.

| Key | Required | Meaning |
|---|---|---|
| `vendor` | yes | literal `"talemetry"` |
| `host` | yes | the careers domain (e.g. `careers.example.com` in committed examples) |
| `name` | no | display company name; authoritative digest company when set |
| `enabled` | no | default `true`; `false` disables without deleting |
| `max_per_employer` | no | reserved/not consumed (unchanged repo-wide) |

**Slug reconstruction**: the single colon-free `host` token is returned
unchanged as the slug (`_reconstruct_slug`), so the `fetch(slug, ...)` contract
is unchanged.

**Company resolution** (`_resolve_company`): `name` if present and non-empty,
else `host` (Talemetry has no tenant; the host is the natural fallback).

**Uniqueness**: `(vendor, slug)` must be unique across the registry, enforced
by the existing duplicate check in `load_registry`.
