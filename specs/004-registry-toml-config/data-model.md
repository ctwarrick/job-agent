# Data Model: source registry

## Entity: `Source` (one `[[source]]` table)

| Field | Type | Req? | Applies to | Notes |
|---|---|---|---|---|
| `vendor` | str | yes | all | Must be a key of `fetch.ADAPTERS` (`greenhouse`/`lever`/`workday`/`icims`). |
| `slug` | str | yes | greenhouse, lever | The board slug, e.g. `initech`. |
| `tenant` | str | yes | workday, icims | Workday/iCIMS tenant. |
| `site` | str | yes | workday | Workday site, e.g. `Globex`. |
| `host` | str | workday: yes; icims: no | workday, icims | Workday host (`wd5`/`wd1`); iCIMS optional custom domain. |
| `name` | str | no | all | Display company name. **Authoritative** (resolved by the loader, passed to the adapter). |
| `enabled` | bool | no (default `true`) | all | `false` → omitted from the loaded list. |
| `max_per_employer` | int | no | all | Reserved; **not wired** this iteration (global env var still governs). |

Unrecognized keys → **raise** (typo guard, FR-003).

## Slug reconstruction (loader output)

| vendor | reconstructed slug |
|---|---|
| greenhouse / lever | `slug` |
| workday | `f"{tenant}:{site}:{host}"` |
| icims | `tenant` if no `host`, else `f"{tenant}:{host}"` |

## Company resolution (loader)

`company = name` if present, else the vendor default: `slug` for
greenhouse/lever, `tenant` for workday/icims. The loader returns
`list[Source]` where `Source = (vendor, slug, company)`; `fetch.main()` passes
`company` to the adapter (FR-002). `companies.toml` and `_resolve_company` are
removed (FR-007).

## Validation rules (all fail-loud, FR-003)

1. `vendor` present and in `ADAPTERS`.
2. Required fields present and non-empty for the vendor (table above).
3. No duplicate reconstructed `(vendor, slug)` across sources.
4. No unrecognized keys on any source.
5. `enabled`, if present, is a bool; `max_per_employer`, if present, is an int.

## Fingerprint-stability rule (FR-005) — LIVE this release

Dedupe fingerprints derive from `company` (`schema.Posting`). The resolved
company replaces the old path *now*, so it MUST match the current effective
company or every posting re-surfaces:
- greenhouse / lever → **omit `name`** so company falls back to the slug
  (e.g. `initech`), exactly as today;
- workday / icims → set `name` to the **exact** former `companies.toml` display
  string (e.g. `globex` → `Globex Corporation`).

The populated `registry.toml` already satisfies this; verify before upload.

## Counts (no personal data quoted)

Populated `registry.toml`: 2 greenhouse + 2 lever + 10 workday = **14 sources**.
