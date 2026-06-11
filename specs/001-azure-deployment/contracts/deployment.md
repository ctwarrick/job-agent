# Contract: Deployment & Infrastructure

**Feature**: `001-azure-deployment` | **Date**: 2026-06-11

Binds `infra/main.bicep` + `infra/main.bicepparam`, `scripts/bootstrap.sh`, and
`.github/workflows/deploy.yml`. Sources: [research.md](research.md) §§1–5, 7–10, 13.

## Bicep parameters (`main.bicepparam`; no secret values)

| Parameter | Example / default | Notes |
|---|---|---|
| `location` | `westus2` | single region |
| `namePrefix` | `jobagent` | resource naming seed |
| `cronExpression` | `0,20,40 11 * * *` | UTC; three ticks ≈20 min apart = retry policy (research §2); derivation formula below |
| `tz` | `America/Los_Angeles` | passed through as `JOBAGENT_TZ` |
| `alertEmail` | maintainer address | action-group email receiver |
| `smsCountryCode` + `smsPhone` | supplied at deploy time | action-group SMS receiver — **never committed**; open question for the human gate |
| `budgetAmount` | `50` | `Microsoft.Consumption/budgets`, alerts at 50% / 80% |
| `imageTag` | git SHA | set by CI on each deploy |
| `maxLlmCalls` | e.g. `10` | passed through as `JOBAGENT_MAX_LLM_CALLS` |
| `retentionDays` | `60` | passed through as `JOBAGENT_RETENTION_DAYS` |

### Cron derivation from deadline + timezone

For delivery deadline `D` in timezone `T`: take the earliest UTC offset `T` uses
across the year (PST = UTC−8 for `America/Los_Angeles`), pick a start hour that
leaves the full retry window (2 extra ticks + run duration) finished comfortably
before `D` in **both** DST phases. Default: deadline 06:00 → ticks at 11:00/11:20/
11:40 UTC (= 03:xx PST / 04:xx PDT). A deadline or timezone change = recompute this
parameter and redeploy; no code change (FR-002).

## Resource inventory (all in `main.bicep`)

| Resource | Purpose |
|---|---|
| Container Apps environment (Consumption) | hosts the job; wired to LAW |
| Container Apps **Job** (`Microsoft.App/jobs`, Schedule trigger, parallelism 1, replicaRetryLimit 0, replicaTimeout ~900 s) | the pipeline; system-assigned MI; Azure Files volume mount; Key Vault secret refs |
| Storage account (no public access) + Azure Files share | `jobs.db` + runtime files |
| Log Analytics workspace | run logs (FR-007) + alert query source |
| Key Vault (standard) | secret store (FR-011); job MI gets *Key Vault Secrets User* |
| Action group | email + SMS receivers (FR-006) |
| Scheduled query alert rule (~25 h lookback, 30-min eval) | fires when `RUN_SUCCESS` is absent — see [runtime-config.md](runtime-config.md) log-marker contract |
| `Microsoft.Consumption/budgets` ($50; 50%/80%) | spend visibility (FR-014) |
| User-assigned managed identity + federated credential | GitHub OIDC deploy identity (created by bootstrap, referenced here) |

## Bootstrap contract (`scripts/bootstrap.sh` — run once per environment)

Inputs: subscription + tenant ID (deploy-time config, never committed), resource
group name, GitHub repo slug. Actions: create resource group; create the
user-assigned managed identity; add the federated credential trusting
`repo:<owner>/job-agent:ref:refs/heads/main`; grant it Contributor (+ RBAC needed
for role assignments) on the resource group. Everything else is `main.bicep`.

**Post-bicep manual steps** (documented in [quickstart.md](../quickstart.md), per
constitution's sanctioned exceptions):

1. Set Key Vault secrets — names fixed by this contract: `anthropic-api-key`,
   `smtp-host`, `smtp-port`, `smtp-user`, `smtp-pass`, `digest-to`.
2. Upload runtime files:
   `az storage file upload --share-name <share> --source profile.md` (likewise
   `screening_prompt.md`, `registry.txt`) — the single sanctioned manual
   production operation (FR-012).
3. Set the Anthropic Console spend limit (suggest $25/mo — maintainer's call).

## GitHub Actions contract (`.github/workflows/deploy.yml`)

Trigger: push to `main`. Permissions: `id-token: write` (OIDC), `packages: write`.

| Job | Depends on | Does |
|---|---|---|
| `test` | — | `uv sync && uv run pytest` |
| `build` | `test` | docker build; push `ghcr.io/<owner>/job-agent:<sha>` (public image) |
| `deploy` | `build` | `az login` via OIDC federated credential; `az deployment group create` with `imageTag=<sha>` |

`needs:` chaining is the no-override deploy gate (FR-008/-009). No secret values in
logs: OIDC has no stored cloud secret, app secrets never transit CI (FR-011).
Whole chain must fit SC-003's 15-minute budget.

## On-demand trigger (FR-019)

```bash
az containerapp job start -n <job> -g <rg>                       # respects skip-if-succeeded
az containerapp job start -n <job> -g <rg> \
  --env-vars JOBAGENT_FORCE=1                                    # force full re-run
```

RBAC-gated by `microsoft.app/jobs/start/action` — only the maintainer's identity.
