# Phase 0 Research: Overnight Run Scaling

Five design unknowns had to be resolved before Phase 1. Each is recorded as
Decision / Rationale / Alternatives.

## R1 — Concurrency model for board-level parallel fetch (FR-007)

**Decision**: `concurrent.futures.ThreadPoolExecutor` with a bounded, configurable worker
count (`JOBAGENT_FETCH_CONCURRENCY`, default 8). Each board's fetch (a direct
`greenhouse`/`lever` call, or a `resilient.run_source` invocation) runs in a worker thread.
Store writes are serialized: each board accumulates its normalized postings in-thread and a
single `upsert_postings` per board is guarded by one module-level `threading.Lock`, so
SQLite is written by one thread at a time.

**Rationale**: The adapters are synchronous `requests` code and the work is
network-I/O-bound — threads release the GIL during socket waits, giving near-linear
wall-clock reduction (SC-004 target ≥3x) with zero adapter rewrites. Stdlib-only, so
Principle IV holds (no new dependency). A `Lock`-guarded upsert keeps the existing SQLite
single-writer assumption intact without per-thread connection plumbing. `concurrency=1`
degenerates to today's sequential order, satisfying the FR-007 reproducibility clause and
giving a clean A/B test oracle.

**Alternatives considered**:
- *asyncio + httpx*: would require rewriting every adapter to async and adding a dependency;
  rejected against Principle IV — the adapters' synchronous `requests` code is the whole
  point of the stdlib-first posture.
- *multiprocessing*: pointless for I/O-bound work and fights the single SQLite file; rejected.
- *Per-thread SQLite connections with WAL*: more moving parts than a single guarded upsert
  for a once-nightly batch; rejected as over-engineering (Principle IV).

## R2 — Cron under DST with a UTC-fixed schedule (FR-002)

**Decision**: Keep a **fixed UTC cron** `0 8,10,12 * * *`. A fixed-UTC schedule with three
attempts 2h apart and a 2h window **cannot** satisfy both halves of FR-002 in both DST
states at once: "all attempts after local midnight" forces the first-attempt UTC hour
H ≥ 8 (PST: H−8 ≥ 0; PDT: H−7 ≥ 0), while "last window ends ≤ 06:00 local" forces H ≤ 7
(PST: (H+4)−8+2 ≤ 6; PDT: (H+4)−7+2 ≤ 6) — unsatisfiable. We prioritize the digest-date
invariant (the spec's stated reason to stay after midnight) over the deadline edge.
`0 8,10,12` puts attempts at 00:00 / 02:00 / 04:00 PST and 01:00 / 03:00 / 05:00 PDT — after
local midnight year-round, so run-start `digest_date` is always the delivery day. The only
cost is at the PDT edge (see Note).

**Rationale**: Container Apps cron is evaluated in UTC with no DST awareness, so a schedule
expressed as "00:00 local" would silently shift an hour twice a year and, in the wrong
direction, push an attempt *before* local midnight — breaking the run-start `digest_date`
computation (the spec's explicit reason for keeping attempts after midnight). Pinning UTC
and choosing H = 8 keeps every attempt after local midnight on both sides, which is the
constraint we refuse to break.

**Note / accepted tradeoff**: At the PDT edge the third attempt (12:00 UTC = 05:00 PDT) plus
a full 2-hour window reaches 07:00 PDT, ~1h past the 06:00 deadline — but only if attempts 1
and 2 both failed AND the third needs >1h (rare with concurrency; a healthy fetch finishes
in minutes). The missed-deadline alert already tolerates a one-evaluation slip to ~06:30.
The rejected alternative `0 7,9,11` would end the third window ≤ 06:00 in both zones but fire
the PST first attempt at 23:00 the *previous* day, breaking `digest_date` every winter night
— a systematic fault traded for a rare one, so declined. This is a `main.bicepparam` value,
not code — a schedule change is a redeploy, consistent with the missed-deadline alert's
coupled-contract note.

**Alternatives considered**:
- *Two seasonal crons or a DST-aware scheduler*: Container Apps offers neither cleanly;
  rejected as complexity for a problem a fixed safe band already solves.
- *Timezone-local cron*: not supported by the platform.

## R3 — How the app learns its execution window for startup validation (FR-004)

**Decision**: Add `JOBAGENT_EXECUTION_WINDOW_SECONDS` (no default; required only when the
fetch budget is set), passed from Bicep as a plain env var mirroring the job's
`replicaTimeoutSeconds`. `main.py` validates at startup, before any external effect:
`fetch_budget_seconds + score_digest_headroom_seconds ≤ execution_window_seconds`; on
failure it `sys.exit()`s with a message naming the three values.

**Rationale**: The container cannot introspect its own Container Apps `replicaTimeout`, so
the window must be told to it. Mirroring it as an env var set from the same Bicep param
keeps a single source of truth (the param) and makes the coherence check a pure, testable
function. Fail-loud-before-effect is Principle V; this is the FR-004 guard that turns a
platform kill into a deploy-time/startup error.

**Alternatives considered**:
- *Derive headroom implicitly instead of validating*: hides misconfiguration until a live
  miss; rejected against Principle V.
- *Query Azure ARM for the timeout at runtime*: adds an Azure API dependency and
  credentials to a stage that needs neither; rejected against Principles I/IV.

## R4 — Least-recently-fully-fetched ordering across all vendors (FR-006)

**Decision**: Order the registry each run by `source_progress.last_converged_at` ascending,
NULLs (never-fetched) first, before dispatch. Extend convergence bookkeeping to the
single-request vendors: after a successful whole-board `greenhouse`/`lever` fetch, call
`mark_converged` (they are always "fully fetched" in one call). A board truncated by the
*stage* budget is simply never dispatched or stops mid-list and does **not** get its
timestamp advanced, so it naturally sorts ahead next run.

**Rationale**: `source_progress` already exists for 006's resilient vendors; reusing it
avoids a second bookkeeping mechanism (Principle IV). Extending it to greenhouse/lever is a
one-line write per successful fetch and makes the ordering total across the whole registry,
which is what the anti-starvation guarantee (SC-003) needs. Ordering by last-converged
ascending means the oldest board always rises to the top, giving the bounded-runs coverage
guarantee for free.

**Alternatives considered**:
- *Separate "deferred queue" table*: duplicates state 006 already tracks; rejected.
- *Round-robin offset into registry order*: does not guarantee the *oldest* board is
  served first, weaker anti-starvation property; rejected (this was spec option C, declined).

## R5 — Stage-budget stop semantics vs. in-flight boards (FR-003, FR-009)

**Decision**: The stage budget is a monotonic-clock deadline computed once at fetch start
(`clock() + JOBAGENT_FETCH_BUDGET_SECONDS`). The dispatch loop stops **submitting** new
boards once the deadline passes; boards already running are allowed to finish (each is
independently bounded by its own 006 per-source deadline, so the worst-case overrun is one
per-source deadline, ~5 min, well inside the 30-min headroom). To keep a single in-flight
board from overrunning the stage budget, each board's effective deadline is
`min(per-source deadline, stage-budget remaining)` — a small helper passed into
`run_source` via its existing `clock`/deadline seam. Boards never submitted are counted as
`deferred` and reported.

**Rationale**: Cleanly stopping *submission* (not hard-cancelling threads) avoids partial
SQLite writes and honors 006's per-source forward-progress guarantees (FR-009): whichever
deadline is nearer bounds the board, and the stage budget bounds their sum. The
`min(...)` clamp closes the one gap where a board submitted just before the deadline could
otherwise run its full per-source budget past the stage stop. Reusing `run_source`'s
existing clock seam means resilient.py semantics are unchanged (FR-007/FR-009).

**Alternatives considered**:
- *Hard-cancel worker futures at the deadline*: risks torn writes and violates 006 forward
  progress; rejected.
- *No clamp (let the last board run its full per-source budget)*: bounded overrun is small,
  but the clamp is cheap and makes the headroom guarantee exact; chose the clamp.

## Consolidated defaults (all env-configurable, fail-loud on invalid — FR-010)

| Knob | Default | Set by |
|---|---|---|
| `replicaTimeoutSeconds` (window) | 7200 (2 h) | `main.bicepparam` |
| `cronExpression` | `0 8,10,12 * * *` (after-midnight both zones, R2) | `main.bicepparam` |
| `JOBAGENT_EXECUTION_WINDOW_SECONDS` | = replicaTimeout (7200) | Bicep env |
| `JOBAGENT_FETCH_BUDGET_SECONDS` | 5400 (90 min) | env |
| `JOBAGENT_SCORE_DIGEST_HEADROOM_SECONDS` | 1800 (30 min) | env |
| `JOBAGENT_FETCH_CONCURRENCY` | 8 | env |
| 006 per-source knobs | unchanged | env |

All new integer knobs use the existing `_positive_int_env` validator (fail loud on
non-positive), consistent with 006 and the score-stage caps.
