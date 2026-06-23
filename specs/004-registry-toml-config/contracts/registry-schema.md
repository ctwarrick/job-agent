# Contract: `registry.toml` schema + loader

## File contract

- Location: `store.data_path("registry.toml")` (honors `JOBAGENT_DATA_DIR`).
- Format: TOML; zero or more `[[source]]` array-of-tables entries.
- Git-ignored runtime file (personal data). Committed schema doc lives in
  `registry.toml.example`.

## Per-source schema

See [data-model.md](../data-model.md) for the field table and validation rules.
Illustrative entry (public example slugs only):

```toml
[[source]]
vendor = "greenhouse"
slug   = "stripe"

[[source]]
vendor = "workday"
name   = "C.H. Robinson"
tenant = "chrobinson"
site   = "CHRobinson"
host   = "wd5"

[[source]]
vendor = "icims"
tenant = "sig"
host   = "careers.sig.com"   # omit for the {tenant}.icims.com default
# enabled = false            # disable without deleting
```

## Loader contract

```python
# src/job_agent/registry.py
@dataclass(frozen=True)
class Source:
    vendor: str
    slug: str       # reconstructed
    company: str    # resolved: name, else slug (gh/lever) / tenant (workday/icims)

def load_registry(path: str | None = None) -> list[Source]: ...
```

- **Returns**: ordered `Source` records; disabled sources omitted (FR-004).
- **Raises**:
  - `tomllib.TOMLDecodeError` — malformed TOML.
  - `ValueError` — unknown vendor, missing required field, duplicate
    `(vendor, slug)`, or unrecognized key; message names the offending source.
- **Side effects**: none (read-only).

## Consumer + adapter contract (changed)

`fetch.main()` iterates `Source` records and dispatches
`ADAPTERS[vendor](source.slug, company=source.company)`. Each adapter gains
`fetch(slug, *, company: str | None = None, timeout=20)` and uses `company`
(default = slug for gh/lever, tenant for workday/icims) instead of
`_resolve_company`. `companies.toml` is retired (FR-007).
