# Data Model: Cloud-Native Scheduled Operation on Azure

**Feature**: `001-azure-deployment` | **Date**: 2026-06-11
**Input**: [spec.md](spec.md) Key Entities, [research.md](research.md) §2, §3, §12

Storage is a single SQLite database (`jobs.db`) on the mounted Azure Files share,
path-prefixed by `JOBAGENT_DATA_DIR` (see
[contracts/runtime-config.md](contracts/runtime-config.md)). Schema management
follows the existing `store.py` pattern: `CREATE TABLE IF NOT EXISTS` DDL plus an
additive `_migrate` step — no migration framework.

## Existing tables (unchanged shape, retained behavior)

### `postings`

| Column | Type | Notes |
|---|---|---|
| `fingerprint` | TEXT PK | sha256(`title\|company\|location` lowercased)[:16] (schema.py) — cross-board dedupe |
| `source` | TEXT | ATS vendor (e.g. `greenhouse`, `lever`) |
| `company` | TEXT | |
| `external_id` | TEXT | vendor-side posting id |
| `title` | TEXT | |
| `location` | TEXT | |
| `description` | TEXT | |
| `url` | TEXT | |
| `posted_at` | TEXT | |
| `fetched_at` | TEXT | drives retention (FR-015) |
| `skills_fit` | INTEGER | NULL = unscored (scoring queue marker) |
| `seniority_fit` | INTEGER | |
| `category_risk` | INTEGER | |
| `rationale` | TEXT | |
| `digest_sent_at` | TEXT | added by `_migrate`; sent-tracking that must survive runs (FR-004) |

Index on `(skills_fit, category_risk)`.

### `applications`

| Column | Type | Notes |
|---|---|---|
| `fingerprint` | TEXT PK → `postings.fingerprint` | |
| `status` | TEXT | `new` \| `dismissed` \| `applied` \| `interviewing` \| `closed`; default `'new'`, seeded on upsert |
| `notes` | TEXT | |
| `updated_at` | TEXT | |

## New table

### `runs` (research §2, §12; spec Key Entity "Run")

One row per job execution (scheduled tick or manual start). This table is the
idempotency lock that turns three cron ticks into "one run + up to 2 retries"
(FR-017, FR-018) and the home of degraded-source reporting (FR-005).

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `digest_date` | TEXT | local-date key, computed via `JOBAGENT_TZ` (default `America/Los_Angeles`) |
| `started_at` | TEXT | UTC timestamp |
| `finished_at` | TEXT | UTC timestamp; NULL while running |
| `outcome` | TEXT | `success` \| `degraded` \| `failed`; NULL while running |
| `attempt` | INTEGER | 1-based count of executions for this `digest_date` |
| `failed_sources` | TEXT (JSON) | list of `{source, company_slug, error}` for sources that failed this run; surfaced in the digest (FR-005) |
| `detail` | TEXT | human-readable failure/degradation summary for log diagnosis (FR-007) |

**Startup check**: if a row exists with this `digest_date` and outcome
`success`/`degraded` (digest was sent), the run exits 0 as a no-op — unless
`JOBAGENT_FORCE=1` (research §13).

## State transitions

```text
Posting:      fetched (skills_fit NULL) ──score──▶ scored ──digest──▶ sent (digest_sent_at set)
Application:  new ──maintainer──▶ dismissed | applied ──▶ interviewing ──▶ closed
Run:          started (outcome NULL) ──▶ success | degraded | failed
```

- `success`: pipeline completed, digest or no-matches notice emailed.
- `degraded`: digest emailed, but ≥1 source failed (`failed_sources` non-empty) or
  scoring stopped at the LLM call cap; degradation is named in the digest body.
- `failed`: aborted before a digest was sent (config/storage/email failure, FR-006);
  a later tick may retry.

## Retention rules (FR-015, research §12.6)

Executed as a purge stage each run:

- Delete `postings` rows where the joined application `status` is `new` or
  `dismissed` **and** `fetched_at` is older than `JOBAGENT_RETENTION_DAYS`
  (default 60); delete the corresponding `applications` rows in the same
  transaction.
- Postings with any other application status are never purged.
- `runs` rows are small and retained indefinitely (revisit if ever material).

## Validation rules (from FRs)

- A digest email is sent on **every** successful run — empty result sets produce
  the no-matches notice, not a skipped send (FR-003).
- `digest_sent_at` is written in the same transaction scope as digest assembly so a
  posting is never emailed twice (FR-004); the share-mounted SQLite file makes this
  durable across executions.
- At most one execution is in flight: ACA job `parallelism: 1` plus the
  `runs`-table no-op check (FR-017, applies to manual starts too, FR-019).
- Scoring stops after `JOBAGENT_MAX_LLM_CALLS` batches; remaining postings stay
  `skills_fit NULL` and are picked up next run (FR-013, spec edge case "LLM scoring
  unavailable mid-run").
