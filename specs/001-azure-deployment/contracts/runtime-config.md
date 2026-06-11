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
| `JOBAGENT_SALARY_FLOOR` | existing | optional | — | scoring tuning |
| `JOBAGENT_DATA_DIR` | **new** | yes (plain env, = share mount path) | `.` | path prefix for `jobs.db` + runtime files; default keeps local dev unchanged |
| `JOBAGENT_TZ` | **new** | yes (plain env) | `America/Los_Angeles` | digest-date computation (stdlib `zoneinfo`); FR-002 timezone configurability |
| `JOBAGENT_MAX_LLM_CALLS` | **new** | yes (plain env) | sensible cap (set in Bicep param) | per-run upper bound on scoring batches (FR-013) |
| `JOBAGENT_RETENTION_DAYS` | **new** | optional | `60` | retention window for unloved postings (FR-015) |
| `JOBAGENT_FORCE` | **new** | no | unset | `1` = bypass the skip-if-already-succeeded check on manual runs (FR-019) |

Secret-valued variables are declared in the ACA job as Key Vault references
resolved by managed identity; non-secret variables are plain env entries in Bicep.
No variable value ever appears in the repository or CI logs (FR-011).

## Exit-code contract

| Exit code | Meaning | Consumer behavior |
|---|---|---|
| `0` | success — digest or no-matches notice sent; **or** no-op skip (this `digest_date` already succeeded) | ACA marks execution Succeeded |
| non-zero | fatal failure (missing config, storage unavailable, email send failure — FR-006); nothing was sent | ACA marks execution Failed; a later cron tick retries (FR-018) |

Per-source fetch failures are **not** fatal and do not affect the exit code; they
are recorded in `runs.failed_sources` and reported in the digest (FR-005).

## Log-marker contract ⚠️ consumed by the alert rule

Emitted to stdout (captured in `ContainerAppConsoleLogs_CL`):

| Marker | When | Consumer |
|---|---|---|
| `RUN_SUCCESS digest_date=<YYYY-MM-DD>` | every successful run, including no-matches days and no-op skips of an already-successful date | absence-of-success scheduled query alert (research §8): no marker in the ~25 h lookback ⇒ action group fires (email + SMS) |
| `RUN_FAILED_FINAL digest_date=<YYYY-MM-DD>` | a fatal failure on the **last** scheduled attempt of the day | optional fast-path alert rule (post-MVP); harmless to emit from day one |

**Changing marker format or emission conditions is a breaking change**: the alert
query in `infra/main.bicep` must change in the same commit, and the pair must be
re-validated per the alert drill in [quickstart.md](../quickstart.md).

Beyond the markers, ordinary logs must satisfy FR-007/SC-005: each run's logs name
the stage reached, per-source outcomes, and the failure cause without re-running.
