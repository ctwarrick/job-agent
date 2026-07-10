# Contract: Runtime Config & Startup Validation

Extends `specs/001-azure-deployment/contracts/runtime-config.md`. New knobs only.

## New environment variables

| Var | Type | Default | Stage | Fails loud when |
|---|---|---|---|---|
| `JOBAGENT_FETCH_CONCURRENCY` | positive int | `8` | fetch | ≤ 0 or non-int |
| `JOBAGENT_FETCH_BUDGET_SECONDS` | positive int | `5400` | fetch | ≤ 0 or non-int |
| `JOBAGENT_SCORE_DIGEST_HEADROOM_SECONDS` | positive int | `1800` | startup | ≤ 0 or non-int |
| `JOBAGENT_EXECUTION_WINDOW_SECONDS` | positive int | none — required; Bicep sets = `replicaTimeoutSeconds` | startup | unset, ≤ 0, non-int, or coherence fails |

All parsed via `resilient._positive_int_env` (or an equivalent shared helper) so invalid
values raise before any external effect (Principle V, FR-010).

## Startup coherence check (FR-004)

In `main.py`, before `store.init()`-driven work has any external effect:

```
if FETCH_BUDGET_SECONDS + SCORE_DIGEST_HEADROOM_SECONDS > EXECUTION_WINDOW_SECONDS:
    sys.exit(
        f"config: fetch budget ({FETCH_BUDGET_SECONDS}s) + score/digest headroom "
        f"({SCORE_DIGEST_HEADROOM_SECONDS}s) exceeds execution window "
        f"({EXECUTION_WINDOW_SECONDS}s)"
    )
```

- Exit is non-zero; nothing is fetched, scored, or emailed.
- Message names all three values (diagnosable from logs without re-running — Principle V).
- This is the guard that converts a would-be platform `DeadlineExceeded` into a
  fail-fast config error.

## Infra pass-through (Bicep)

`infra/main.bicep` adds an env entry on the job container:

```
{ name: 'JOBAGENT_EXECUTION_WINDOW_SECONDS', value: string(replicaTimeoutSeconds) }
```

so the app validates against the same value the platform enforces (single source of truth =
the `replicaTimeoutSeconds` param). `main.bicepparam` pins the designed end state:

```
param replicaTimeoutSeconds = 7200          // 2-hour window (US1)
param cronExpression = '0 8,10,12 * * *'    // after local midnight both zones (R2)
```

Both re-validated by `scripts/validate-infra.sh` (quality gate #2 for infra changes).

## Coupled-contract note

The `cronExpression` / `replicaTimeoutSeconds` pair and the missed-deadline alert's
06:00-local deadline are coupled: a schedule change is a **redeploy**, not a query edit,
and must keep every attempt after local midnight in both PST and PDT (research R2). Changing
the band without re-checking DST safety is a regression.
