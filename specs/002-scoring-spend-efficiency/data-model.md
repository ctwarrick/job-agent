# Phase 1 Data Model: LLM Scoring Spend Efficiency

**Feature**: `002-scoring-spend-efficiency` | **Date**: 2026-06-12 | **Plan**: [plan.md](plan.md)

Derived from the spec's Key Entities and the design in [research.md](research.md).
The only persistent schema change is one additive column on `postings`; the rest
are in-memory entities (criteria, budget, summary) that live for the duration of
a run.

---

## Posting scoring lifecycle

A posting moves through an explicit, at-most-once lifecycle so its disposition is
always explainable (spec Key Entities; SC-004, SC-006):

```text
              upsert (fetch stage)
                     │
                     ▼
                 unscored          skills_fit IS NULL AND filter_reason IS NULL
                  /      \
       classify()=reason   classify()=None
                │              │
                ▼              ▼
          filtered-out      scored (LLM)
        filter_reason set   skills_fit … rationale set
```

- **unscored**: `skills_fit IS NULL AND filter_reason IS NULL` — eligible for the
  filter on the next run. This is the new **scorable** set.
- **filtered-out**: `filter_reason IS NOT NULL` (and `skills_fit IS NULL`) — the
  deterministic gate rejected it; it carries a machine-readable reason and is
  never re-examined by the filter or the LLM (FR-003, SC-006).
- **scored**: `skills_fit IS NOT NULL` — the LLM scored it; unchanged from today.

A posting is processed **at most once**: once it has either a `filter_reason` or a
`skills_fit`, no later run re-sends it (SC-006). The two terminal states are
mutually exclusive in practice (a rejected posting is never sent to the LLM).

---

## Schema change: `postings.filter_reason`

One additive, nullable column on the existing `postings` table:

| Column | Type | NULL means | Set by | Example value |
|---|---|---|---|---|
| `filter_reason` | `TEXT` | not filtered (still scorable or already scored) | `store.record_filter_rejections()` | `function_denylist:sales`, `age:47d`, `location:Berlin` |

**Migration** — additive `ALTER`, idempotent, following the existing
`digest_sent_at` pattern in [`store._migrate`](../../src/job_agent/store.py#L135):

```python
cols = {r["name"] for r in conn.execute("PRAGMA table_info(postings)")}
if "filter_reason" not in cols:
    conn.execute("ALTER TABLE postings ADD COLUMN filter_reason TEXT")
```

No backfill: pre-existing rows get `filter_reason = NULL`. Already-scored rows
(`skills_fit IS NOT NULL`) stay excluded from scoring by the existing
`skills_fit`-based predicate regardless of the new column, so the migration is
safe on the live `jobs.db` mid-backlog (366 already-scored rows untouched).

The `DDL` `CREATE TABLE` for fresh databases also gains the column inline (so a
brand-new `jobs.db` and a migrated one converge), mirroring how the column is
both in `DDL` and `_migrate` is the existing convention.

---

## `store.py` additions

Three additions, all additive (no signature changes to existing functions):

### `scorable(path=None) -> list[sqlite3.Row]`

The new resume/eligibility query — the scorable set defined above:

```sql
SELECT * FROM postings WHERE skills_fit IS NULL AND filter_reason IS NULL
```

`score.main()` switches from `store.unscored()` to `store.scorable()` so that
filter-rejected rows are not re-fetched into the run (FR-003, SC-006).

> `unscored()` (`skills_fit IS NULL`) is **kept** unchanged — it has existing
> tests and may have other readers; `scorable()` is the narrower successor the
> score loop uses. Keeping both avoids touching `unscored`'s contract.

### `record_filter_rejections(rejections, path=None) -> None`

Persist the filter's verdicts in one transaction. `rejections` is an iterable of
`(fingerprint, reason)` pairs:

```sql
UPDATE postings SET filter_reason=? WHERE fingerprint=?
```

Called once per run after the filter pass, before the LLM loop, so rejected rows
are durably out of scope even if the LLM loop later cap-stops or crashes
(FR-003, FR-007).

### `_migrate` extension

Add the `filter_reason` guard shown above to the existing `_migrate(conn)`.

**Digest unaffected (FR-013)**: the digest query already filters on
`skills_fit IS NOT NULL`, so filter-rejected rows (`skills_fit IS NULL`) are
naturally excluded — no digest change is needed.

---

## In-memory entities (per run, not persisted)

### Filter criteria (`filter.Criteria`)

Loaded once per run from `filter.toml` by `filter.load_criteria`. A small frozen
value object (see [contracts/filter-criteria.md](contracts/filter-criteria.md)):

| Field | Type | Source key | Default |
|---|---|---|---|
| `denylist_title_keywords` | `tuple[str, ...]` | `[denylist].title_keywords` | — (required, may be empty) |
| `allowlist_title_keywords` | `tuple[str, ...]` | `[allowlist].title_keywords` | `()` (advisory) |
| `age_max_days` | `int` | `[age].max_days` | `30` |
| `location_remote_ok` | `bool` | `[location].remote_ok` | `true` |
| `location_regions` | `tuple[str, ...]` | `[location].regions` | `()` (state/region token keep-list) |
| `location_metros` | `tuple[str, ...]` | `[location].metros` | `()` (substring keep-list) |

Validated at load (§4): keyword fields must be lists of strings, `max_days` a
positive int, `remote_ok` a bool. Malformed ⇒ `sys.exit` before any LLM call.

### Run budget (`Budget`)

Operational limits + pricing, read from env once per run
([contracts/runtime-config.md](contracts/runtime-config.md)):

| Field | Env var | Default |
|---|---|---|
| `max_postings` | `JOBAGENT_MAX_POSTINGS_PER_RUN` | `200` |
| `max_cost` | `JOBAGENT_MAX_COST_PER_RUN` | `5.00` |
| `price_input` | `JOBAGENT_PRICE_INPUT` | `3.00` /MTok |
| `price_output` | `JOBAGENT_PRICE_OUTPUT` | `15.00` /MTok |
| `price_cache_write` | `JOBAGENT_PRICE_CACHE_WRITE` | `3.75` /MTok |
| `price_cache_read` | `JOBAGENT_PRICE_CACHE_READ` | `0.30` /MTok |

Invalid (non-numeric / ≤ 0 cap / < 0 price) ⇒ `sys.exit` before any LLM call.

### Run summary (`RunSummary`)

Accumulated during the run, emitted once to stdout as `SCORE_SUMMARY` (and, on a
cap stop, preceded by `SCORE_CAP_STOP`). Not persisted — logs are the record
(FR-011, SC-005):

| Field | Meaning |
|---|---|
| `fetched` | postings considered this run (the scorable set size at start) |
| `filtered` | total rejected by the filter this run |
| `filtered_by_reason` | breakdown, e.g. `{function_denylist: 312, age: 40, location: 18}` |
| `scored` | postings sent to the LLM and scored this run |
| `remaining` | scorable postings still unscored after a cap stop (else 0) |
| `input_tokens` / `output_tokens` | summed `usage` across batches |
| `cache_creation_input_tokens` / `cache_read_input_tokens` | summed `usage` across batches |
| `estimated_cost` | dollars, computed from the prices above (research §7) |
| `cap_stop` | which limit bound, if any: `postings` \| `cost` \| `none` |

---

## Invariants

- **I1 (at-most-once, SC-006)**: no posting with a non-NULL `skills_fit` or a
  non-NULL `filter_reason` is ever re-sent to the filter or the LLM. Enforced by
  the `scorable()` predicate.
- **I2 (every drop has a reason, SC-004)**: any posting not sent to the LLM that
  was *considered* this run has a non-NULL `filter_reason` — there is no silent
  drop. Fail-open gates (age/location with missing fields) and the advisory
  allowlist never produce a NULL-reason drop.
- **I3 (cap honored, SC-002)**: `scored` this run ≤ `max_postings`, and projected
  `estimated_cost` ≤ `max_cost` (pre-call projection, research §5).
- **I4 (additive migration safety)**: applying `_migrate` to the live
  mid-backlog `jobs.db` changes no existing `skills_fit` value and re-running it
  is a no-op.
