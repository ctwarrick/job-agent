# Contract: Talemetry / TTC-Portals Adapter

The interfaces this feature exposes to the rest of the pipeline. The CLI
command surface (`jobagent-fetch`, `main.py`) is unchanged; this adapter plugs
into the existing dispatch.

## Adapter function — `src/job_agent/adapters/talemetry.py`

```python
def fetch(slug: str, *, company: str | None = None,
          timeout: int = 20) -> list[Posting]:
    ...
```

- **`slug`**: the careers `host` (e.g. `careers.example.com`), returned
  unchanged by the registry's slug reconstruction.
- **`company`**: display company name; **falls back to `host`** when absent
  (FR-009), so the fingerprint component is never empty.
- **`timeout`**: per-request timeout in seconds (default 20), matching the
  other adapters.
- **Returns**: a list of US-scoped normalized `Posting` objects (possibly
  empty).
- **Raises**: `requests`-layer transport errors propagate so `fetch.main`'s
  per-source guard records the failure and the run continues (FR-007). A
  zero-posting parse does **not** raise — it warns and returns `[]` (FR-014).

### Behavioral contract

| # | Given | Then |
|---|---|---|
| C1 | a listing with `/jobs/{id}-{slug}/` entries | one normalized `Posting` per entry, with the required scorer/dedupe fields (FR-001, FR-003) |
| C2 | a listing entry missing the description | the detail page is fetched for it (FR-013) |
| C3 | a location positively identified as non-US | the entry is dropped before scoring; US/ambiguous/empty are retained (FR-005) |
| C4 | `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` set below the board size | at most the cap is returned and paging stops at the cap (FR-006) |
| C5 | the cap unset | all parsed postings returned, no artificial cap (FR-006) |
| C6 | the same posting on a later run | one stored row — content fingerprint collapses the duplicate (SC-004) |
| C7 | a parse yielding zero postings | `[]` returned and a distinct stderr warning emitted (FR-014, SC-007) |
| C8 | a listing entry with no parseable numeric ID | that entry skipped with a warning; others retained (edge case) |
| C9 | an HTTP error from the source | the exception propagates to `fetch.main` (FR-007) |
| C10 | multiple paginated/detail requests | a politeness sleep occurs between them (FR-008) |

`requests` and `time` are imported at module level so tests can monkeypatch
`talemetry.requests` and `talemetry.time.sleep`; the BeautifulSoup parse runs
on canned HTML offline (no patching needed).

## Dispatch registration — `src/job_agent/fetch.py`

```python
from .adapters import greenhouse, icims, lever, talemetry, workday

ADAPTERS = {
    ...
    "talemetry": talemetry.fetch,
}
```

## Registry schema additions — `src/job_agent/registry.py`

```python
_REQUIRED_FIELDS["talemetry"] = ("host",)
_VENDOR_FIELDS["talemetry"]   = {"host"}
# _reconstruct_slug: talemetry -> raw["host"]
# _resolve_company:  talemetry -> name if set else raw["host"]
```

Validation reuses the existing fail-loud path: an unknown key on a talemetry
source, or a missing/empty `host`, raises `ValueError` at load time naming the
offending source.

## Committed example — `registry.toml.example`

A `talemetry` `[[source]]` block using the placeholder host
`careers.example.com` and a placeholder `name`. No real employer name (FR-012,
SC-006).

## Dependency manifest — `pyproject.toml` + `uv.lock`

`beautifulsoup4>=4.12` added to `[project].dependencies`; `uv lock` re-run so
the lock is committed alongside.
