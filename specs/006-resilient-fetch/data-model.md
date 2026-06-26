# Phase 1 Data Model: Resilient, Time-Bounded ATS Fetching

## Posting (unchanged)

`schema.Posting` is **not modified**. Identity stays the content fingerprint
`sha256(title|company|location|description)[:16]` (the `postings` PRIMARY KEY).
Two *runtime* states of a Posting matter to this feature, but only the second is
ever persisted:

| State | Fields | Persisted? |
|---|---|---|
| **Listing stub** | title, location, posted_at, url, external_id, company; `description=""` | No — exists only inside `resilient.run_source` until a description is fetched |
| **Described posting** | full stub + retrieved `description` | Yes — `store.upsert_postings`, fingerprint final at insert |

Storing only described postings keeps the fingerprint stable (R3/D3) and honors
FR-009 (no re-key). For iCIMS the listing stub already carries its inline
description, so it is effectively "described" on arrival.

## postings table (unchanged columns; new query)

No column change. The feature relies on the existing `external_id` column as the
**description-independent, per-source identity** for forward progress:

- `existing_external_ids(source, company) -> set[str]` —
  `SELECT external_id FROM postings WHERE source=? AND company=?`. The set of
  external_ids already described+stored for a source; the orchestration skips these
  so each run advances to new survivors (FR-015 forward progress).

`upsert_postings` keeps `INSERT OR IGNORE` (idempotent, no duplicate rows — edge
case "re-fetching an already-ingested board") but its **return value is corrected**
to count only the postings insert, not the companion applications insert (FR-011).

## source_progress table (new — additive migration)

```sql
CREATE TABLE IF NOT EXISTS source_progress (
    source            TEXT NOT NULL,
    company           TEXT NOT NULL,
    last_converged_at TEXT,          -- ISO-8601 UTC; NULL until first convergence
    PRIMARY KEY (source, company)
);
```

Created in `store.init()` via the existing idempotent DDL/`_migrate` path — purely
additive, no data touched, satisfying FR-009 ("no destructive migration").

Helpers:

- `get_last_converged(source, company) -> str | None`
- `mark_converged(source, company, when)` — upsert `last_converged_at = when`;
  called when a source finishes a run with `remaining == 0`.
- `seed_source(source, company, when)` — insert `last_converged_at = when` if the
  source has no row yet (first sighting → full grace window).

### Convergence / staleness lifecycle

```
first run for a source ─► seed last_converged_at = now (grace window starts)
run finishes source with remaining == 0 ─► mark_converged(now)   [fully caught up]
run leaves remaining > 0:
    if now - last_converged_at <= STALENESS_BOUND ─► partial/degraded (this run)
    if now - last_converged_at  > STALENESS_BOUND ─► PERSISTENTLY degraded (alert)
```

## SourceResult (new in-memory record, returned by resilient.run_source)

| Field | Meaning |
|---|---|
| `source`, `company_slug` | identity for digest/run-row reporting |
| `new` | count of newly described+stored postings this run (feeds FR-011 total) |
| `skipped` | per-item detail/page failures skipped (FR-005) |
| `truncated` | bool — backstop (cap or deadline) fired this run (FR-013) |
| `remaining` | survivors still un-described after this run (forward-progress backlog) |
| `persistent` | bool — truncated **and** stuck beyond the staleness bound (FR-015) |
| `error` | set only for a wholly-failed source (existing FR-006 path) |

`fetch.main` aggregates `SourceResult`s into: the per-source log line, the total
new count, the existing `failed_sources` list (for `error`), and a new
`partial_sources` list (for `truncated`/`skipped`/`persistent`) passed to the
digest.

## Validation rules / invariants

- A posting is stored **only** with a non-empty fingerprint computed from a present
  description (stubs never persisted) — preserves identity (R3).
- A filter-rejected posting from an in-scope adapter is **not** stored and incurs
  **no** description fetch (FR-003); it is re-derived from the listing each run.
- A survivor whose `fetch_description` fails is **not** stored (left to retry next
  run) and never scored on empty text (FR-010); it counts toward `skipped`.
- `existing_external_ids` membership ⇒ never re-described (monotonic progress,
  accepted no-change-detection tradeoff, R3).
- `source_progress` rows are additive and survive across runs; purging postings does
  not delete progress rows (a re-emptied board simply re-converges).
