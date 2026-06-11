# Data Model: Cloud-Native Scheduled Operation on Azure

**Feature**: `001-azure-deployment` | **Date**: 2026-06-11
**Input**: [spec.md](spec.md) Key Entities, [research.md](research.md) §2, §3, §12

Storage is a single SQLite database (`jobs.db`) on the mounted Azure Files share,
path-prefixed by `JOBAGENT_DATA_DIR` (see
[contracts/runtime-config.md](contracts/runtime-config.md)). Schema management
follows the existing `store.py` pattern: `CREATE TABLE IF NOT EXISTS` DDL plus an
additive `_migrate` step — no migration framework.

## Existing tables (shape unchanged; fingerprint identity revised — see below)

### `postings`

| Column | Type | Notes |
|---|---|---|
| `fingerprint` | TEXT PK | sha256(`title\|company\|location\|description` lowercased)[:16] (schema.py) — content-identity dedupe; see [Dedupe identity revision](#dedupe-identity-revision) |
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
| `status` | TEXT | `new` \| `dismissed` \| `duplicate` \| `applied` \| `interviewing` \| `closed`; default `'new'`, seeded on upsert. `duplicate` = maintainer-flagged re-surfaced posting (see below); excluded from digests like `dismissed` |
| `notes` | TEXT | |
| `updated_at` | TEXT | |

## Dedupe identity revision

The fingerprint gains the description as a fourth component:
`sha256(title|company|location|description, all lowercased)[:16]`, where
description is the post-`_clean` text stored on the row (HTML stripped,
whitespace collapsed). Under the old `title|company|location` key, two
genuinely distinct openings sharing a title at one company/location collapsed
to one row: `INSERT OR IGNORE` silently dropped the second posting, and
dismissing one suppressed the other (and any future re-post) forever.
Including the description keeps distinct same-title requisitions as separate
rows while still collapsing exact cross-board re-posts.

**Accepted failure mode (by design — do not "fix" without revisiting this
section)**: any edit to a posting's description — or board-specific
boilerplate on a cross-post — changes the fingerprint, so an already-seen
role re-surfaces in a later digest as new. We accept this because the failure
is *visible and correctable* (a familiar posting shows up again), whereas the
old key's failure was *silent data loss* (a real posting never seen at all).
The remedy is the `duplicate` application status: the maintainer flags the
re-surfaced row, which removes it from digest eligibility permanently, same
mechanics as `dismissed`.

**Migration**: `_migrate` recomputes every existing fingerprint from stored
columns (the description is on the row) and rewrites `postings.fingerprint`
and the matching `applications.fingerprint` in one transaction. The new key
strictly subdivides the old one, so recomputation can never merge rows;
scores, statuses, and sent-tracking carry over, and the migration itself
re-surfaces nothing.

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

**Startup check** (two rules, evaluated in order):

1. **In-flight lock (FR-017)**: a row for this `digest_date` with outcome NULL
   and `started_at` within the replica timeout (~900 s) means another execution
   is running → exit 0 as a no-op. Applies to scheduled *and* manual starts, and
   is **not** bypassed by `JOBAGENT_FORCE`. A NULL-outcome row *older* than the
   timeout is a crashed attempt: mark it `failed` and proceed.
2. **Already-succeeded skip**: a row with this `digest_date` and outcome
   `success`/`degraded` (digest was sent) → exit 0 as a no-op — unless
   `JOBAGENT_FORCE=1` (research §13).

## State transitions

```text
Posting:      fetched (skills_fit NULL) ──score──▶ scored ──digest──▶ sent (digest_sent_at set)
Application:  new ──maintainer──▶ dismissed | duplicate | applied ──▶ interviewing ──▶ closed
Run:          started (outcome NULL) ──▶ success | degraded | failed
```

- `success`: pipeline completed, digest or no-matches notice emailed.
- `degraded`: digest emailed, but ≥1 source failed (`failed_sources` non-empty) or
  scoring stopped at the LLM call cap; degradation is named in the digest body.
- `failed`: aborted before a digest was sent (config/storage/email failure, FR-006);
  a later tick may retry.

## Retention rules (FR-015, research §12.6)

Executed as a purge stage each run:

- Delete `postings` rows where the joined application `status` is `new`,
  `dismissed`, or `duplicate` **and** `fetched_at` is older than `JOBAGENT_RETENTION_DAYS`
  (default 60); delete the corresponding `applications` rows in the same
  transaction.
- Postings with any other application status are never purged.
- `runs` rows are small and retained indefinitely (revisit if ever material).

## Validation rules (from FRs)

- A digest email is sent on **every** successful run — empty result sets produce
  the no-matches notice, not a skipped send (FR-003).
- `digest_sent_at` and the run's success outcome are committed immediately
  *after* a confirmed email send, in one transaction; the share-mounted SQLite
  file makes this durable across executions (FR-004). A crash between the send
  and that commit means the retry re-sends: delivery is at-least-once, and the
  rare duplicate digest is accepted by design (spec edge case).
- At most one execution is in flight: ACA job `parallelism: 1` plus the
  `runs`-table no-op check (FR-017, applies to manual starts too, FR-019).
- Scoring stops after `JOBAGENT_MAX_LLM_CALLS` batches; remaining postings stay
  `skills_fit NULL` and are picked up next run (FR-013, spec edge case "LLM scoring
  unavailable mid-run").
