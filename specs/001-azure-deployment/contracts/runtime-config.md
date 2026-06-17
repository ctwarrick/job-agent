# Contract: Runtime Configuration

**Feature**: `001-azure-deployment` | **Date**: 2026-06-11

The application's external interface is environment variables in, exit code +
structured log markers out. This contract binds the app, the Bicep job definition,
and the alert rule; changing any marked item is a breaking change requiring
coordinated updates (see [deployment.md](deployment.md)).

## Environment variables

| Variable | New? | Required in prod | Default | Purpose |
|---|---|---|---|---|
| `ANTHROPIC_API_KEY` | existing | yes (Key Vault) | — | LLM scoring auth |
| `SMTP_HOST` | existing | yes (Key Vault) | — | digest delivery |
| `SMTP_PORT` | existing | yes (Key Vault) | — | digest delivery |
| `SMTP_USER` | existing | yes (Key Vault) | — | digest delivery |
| `SMTP_PASS` | existing | yes (Key Vault) | — | digest delivery |
| `DIGEST_TO` | existing | yes (Key Vault) | — | recipient address |
| `JOBAGENT_MODEL` | existing | yes (plain env) | `claude-sonnet-4-6` (bumped from retiring `claude-opus-4-20250514`) | scoring model; cost dial |
| `JOBAGENT_SALARY_FLOOR` | existing | yes (Key Vault, secret name `salary-floor`) | — | scoring tuning; score stage `sys.exit`s without it |
| `JOBAGENT_DATA_DIR` | **new** | yes (plain env, = share mount path) | `.` | path prefix for `jobs.db` + runtime files; default keeps local dev unchanged |
| `JOBAGENT_TZ` | **new** | yes (plain env) | `America/Los_Angeles` | digest-date computation (stdlib `zoneinfo`); FR-002 timezone configurability |
| `JOBAGENT_MAX_LLM_CALLS` | **new** | yes (plain env) | sensible cap (set in Bicep param) | per-run upper bound on scoring batches (FR-013) |
| `JOBAGENT_RETENTION_DAYS` | **new** | optional | `60` | retention window for postings (FR-015) |
| `JOBAGENT_FORCE` | **new** | no | unset | `1` = bypass the skip-if-already-succeeded check on manual runs (FR-019); does **not** bypass the in-flight lock (FR-017) |

Secret-valued variables are declared in the ACA job as Key Vault references
resolved by managed identity; non-secret variables are plain env entries in Bicep.
No variable value ever appears in the repository or CI logs (FR-011).

## Exit-code contract

| Exit code | Meaning | Consumer behavior |
|---|---|---|
| `0` | success — digest or no-matches notice sent; **or** no-op skip (this `digest_date` already succeeded, or a run for it is in flight) | ACA marks execution Succeeded |
| non-zero | fatal failure (missing config, storage unavailable, email send failure — FR-006); nothing was sent | ACA marks execution Failed; a later cron tick retries (FR-018) |

Per-source fetch failures are **not** fatal and do not affect the exit code; they
are recorded in `runs.failed_sources` and reported in the digest (FR-005).

## Log-marker contract ⚠️ consumed by the alert rule

Emitted to stdout (captured in `ContainerAppConsoleLogs_CL`):

| Marker | When | Consumer |
|---|---|---|
| `RUN_SUCCESS digest_date=<YYYY-MM-DD>` | every successful run, including no-matches days and no-op skips of an already-successful date; emitted only **after** the email send is confirmed (or after the skip decision) | missed-deadline scheduled query alert ([deployment.md](deployment.md) resource inventory): fires at the first evaluation past the local delivery deadline on a day with no `RUN_SUCCESS` carrying **that day's** `digest_date` ⇒ action group (email + SMS). The `digest_date` key is what stops markers from evening manual runs or no-op skips masking a failed overnight run |
| `RUN_FAILED_FINAL digest_date=<YYYY-MM-DD>` | a fatal failure on the **last** scheduled attempt of the day | optional fast-path alert rule (post-MVP); harmless to emit from day one |

**Changing marker format or emission conditions is a breaking change**: the alert
query in `infra/main.bicep` must change in the same commit, and the pair must be
re-validated by the alert drill in [quickstart.md](../quickstart.md). The drill is
**required** at initial provisioning (and on rebuild) and after any change to the
markers or the alert query — not optional hardening.

Ordering note (spec edge case "crash between email send and state commit"):
`RUN_SUCCESS` and the database success state are written only after a confirmed
send, so a crash inside that window causes a retry and possibly a duplicate
digest — at-least-once delivery, by design.

Beyond the markers, ordinary logs must satisfy FR-007/SC-005. Minimum content per
run: the stage reached (fetch / score / digest / purge), per-source outcome with
error text for each failed source, counts (fetched, scored, still queued), and on
fatal failure the failing stage, the exception, and the config key or resource
involved. Logs never contain secret values, runtime-file contents, or scoring
rationale (FR-007); naming a registry company in per-source status is accepted —
the log store is private.
