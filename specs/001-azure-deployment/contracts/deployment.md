# Contract: Deployment & Infrastructure

**Feature**: `001-azure-deployment` | **Date**: 2026-06-11

Binds `infra/main.bicep` + `infra/main.bicepparam`, `scripts/bootstrap.sh`, and
`.github/workflows/deploy.yml`. Sources: [research.md](research.md) §§1–5, 7–10, 13.

## Bicep parameters (`main.bicepparam`; no secret values)

| Parameter | Example / default | Notes |
|---|---|---|
| `location` | `westus2` | single region; accepted default, no hard requirement — any region with Container Apps Jobs works, override at deploy |
| `namePrefix` | `jobagent` | resource naming seed |
| `cronExpression` | `0,20,40 11 * * *` | UTC; three ticks ≈20 min apart = retry policy (research §2); derivation formula below |
| `tz` | `America/Los_Angeles` | passed through as `JOBAGENT_TZ` |
| `deliveryDeadlineHourLocal` | `6` | local hour (0–23) in `tz` after which a day with no `RUN_SUCCESS` is "missed"; feeds the alert query. A deadline change is a redeploy, not a query edit (FR-002) |
| `alertEmail` | supplied at deploy time | action-group email receiver — **never committed** (see below) |
| `smsCountryCode` + `smsPhone` | supplied at deploy time | action-group SMS receiver — **never committed** (see below) |
| `budgetAmount` | `50` | `Microsoft.Consumption/budgets`, alerts at 50% / 80% |
| `imageTag` | git SHA | set by CI on each deploy |
| `maxPostingsPerRun` | `200` | passed through as `JOBAGENT_MAX_POSTINGS_PER_RUN` — per-run scoring cap (Feature 002; supersedes the never-implemented `maxLlmCalls`/`JOBAGENT_MAX_LLM_CALLS`, see [002 runtime-config](../../002-scoring-spend-efficiency/contracts/runtime-config.md)) |
| `maxCostPerRun` | `'5.00'` | passed through as `JOBAGENT_MAX_COST_PER_RUN` — per-run estimated-dollar cap (Feature 002) |
| `retentionDays` | `60` | passed through as `JOBAGENT_RETENTION_DAYS` |

### Never-committed parameters

`alertEmail`, `smsCountryCode`, and `smsPhone` are personal data: their
**values** never appear in the repo (spec Assumptions: no email addresses or
phone numbers in the public repository). `main.bicepparam` assigns them with
`readEnvironmentVariable(...)`, so the file references only the variable *names*;
the values are read from the environment at compile time and are supplied on
**every** deployment:

- bootstrap deploy: exported as env vars (`ALERT_EMAIL`, `SMS_COUNTRY_CODE`,
  `SMS_PHONE`) in the maintainer's shell before `az deployment group create`;
- CI deploys: the same-named GitHub Actions repository secrets, exposed as env
  vars on the deploy step, so routine redeploys preserve the receivers without
  recording them anywhere in the repo.

A `.bicepparam` file is compiled before inline `--parameters` merge, so a
required (no-default) param must be assigned in the param file itself (else Bicep
BCP258); the environment read satisfies that without committing a value, and a
missing variable fails the compile (BCP427) rather than building a receiver-less
alert.

For FR-010 reproducibility, GitHub Actions repository secrets count as part of
"secrets": the environment is reproducible from repository + secrets + runtime
files.

### Cron derivation from deadline + timezone

For delivery deadline `D` in timezone `T`: take the earliest UTC offset `T` uses
across the year (PST = UTC−8 for `America/Los_Angeles`), pick a start hour that
leaves the full retry window (2 extra ticks + run duration) finished comfortably
before `D` in **both** DST phases. Default: deadline 06:00 → ticks at 11:00/11:20/
11:40 UTC (= 03:xx PST / 04:xx PDT). A deadline or timezone change = recompute this
parameter and redeploy; no application code change (FR-002).

Two arithmetic constraints make FR-017/FR-018 hold:

- **Spacing**: the 20-minute tick interval sits inside FR-018's "approximately
  15–30 minutes" window, and MUST exceed `replicaTimeout` (~900 s) so one
  scheduled attempt can never overlap the next.
- **Deadline**: worst case, the last tick starts 04:40 PDT and is force-stopped at
  the ~900 s timeout → finished by ~04:55, over an hour before the 06:00 deadline,
  satisfying FR-018's "every attempt completes before the deadline" in both DST
  phases. A run exceeding the timeout is killed, marked Failed, and retried by the
  next tick (or alerted after the last).

## Resource inventory (all in `main.bicep`)

| Resource | Purpose |
|---|---|
| Container Apps environment (Consumption) | hosts the job; wired to LAW |
| Container Apps **Job** (`Microsoft.App/jobs`, Schedule trigger, parallelism 1, replicaRetryLimit 0, replicaTimeout ~900 s) | the pipeline; system-assigned MI; Azure Files volume mount; Key Vault secret refs |
| Storage account (no public access) + Azure Files share | `jobs.db` + runtime files |
| Log Analytics workspace | run logs (FR-007) + alert query source |
| Key Vault (standard) | secret store (FR-011); job MI gets *Key Vault Secrets User* |
| Action group | email + SMS receivers (FR-006) |
| Scheduled query alert rule (30-min evaluation) | **missed-deadline semantics**: returns a failure row only when local time in `tz` is past the delivery deadline (`deliveryDeadlineHourLocal`) *and* no `RUN_SUCCESS digest_date=<today's local date>` exists. Keying on `digest_date` means markers from evening manual runs or no-op skips (which carry a different/already-passed date) cannot mask a failed overnight run; computing in local time keeps it DST-correct. Worst-case notification = deadline + one 30-min evaluation ≈ 06:30, which is SC-004's bound. Condition self-clears at local midnight or on a successful recovery run. See [runtime-config.md](runtime-config.md) log-marker contract |
| `Microsoft.Consumption/budgets` ($50; 50%/80%) | spend visibility (FR-014) |
| User-assigned managed identity + federated credential | GitHub OIDC deploy identity (created by bootstrap, referenced here) |

## Identity & access matrix (FR-022)

Least privilege is a requirement, not an accident of implementation. Any new
permission means updating this table in the same change.

| Identity | Holds | Deliberate acceptance |
|---|---|---|
| GitHub-OIDC deploy identity (UAMI) | Contributor + role-assignment rights on the **resource group only**; usable only via the federated credential bound to `repo:<owner>/job-agent:ref:refs/heads/main` | Contributor implies storage key listing — accepted for a solo project: the identity is trusted with the environment it creates, and never used interactively |
| Job runtime identity (system-assigned MI) | *Key Vault Secrets User* on the vault; Azure Files share access via the platform-managed mount | no role on any resource beyond these two |
| Maintainer | subscription owner; the only human identity, and the only one expected to invoke `microsoft.app/jobs/start/action` (FR-019) | the deploy UAMI's Contributor technically includes job-start; it has no code path that calls it |
| Action group | outbound notification only (email + SMS receivers) | — |

No identity outside this table has access to the storage share, the database, the
Key Vault, or the on-demand trigger; the storage account and Key Vault allow no
anonymous/public access (FR-012, US4).

## Bootstrap contract (`scripts/bootstrap.sh` — run once per environment)

Inputs: subscription + tenant ID (deploy-time config, never committed), resource
group name, GitHub repo slug. Actions: create resource group; create the
user-assigned managed identity; add the federated credential trusting
`repo:<owner>/job-agent:ref:refs/heads/main`; grant it Contributor (+ RBAC needed
for role assignments) on the resource group. Everything else is `main.bicep`.

**Post-bicep manual steps** (documented in [quickstart.md](../quickstart.md)):

1. Set Key Vault secrets — names fixed by this contract: `anthropic-api-key`,
   `smtp-host`, `smtp-port`, `smtp-user`, `smtp-pass`, `digest-to`.
2. Upload runtime files:
   `az storage file upload --share-name <share> --source profile.md` (likewise
   `screening_prompt.md`, `registry.txt`).
3. Set an Anthropic Console spend **notification** (e.g. at $25/mo) — not a hard
   limit. The spec's policy is alert-only: a hard limit would silently halt
   scoring mid-run, and the per-run call bound (FR-013) is already the runaway
   protection.

**Classification against the constitution's "single sanctioned manual
operation"**: steps 1–3 (plus bootstrap) are *one-time provisioning*, repeated
only on a rebuild (US5) or secret rotation. The runtime-file upload in step 2 is
also the one *recurring* manual production operation — the sanctioned FR-012
mechanism used whenever `profile.md`/`screening_prompt.md`/`registry.txt` change.
Nothing else in steady-state operation is manual.

## GitHub Actions contract (`.github/workflows/deploy.yml`)

Trigger: push to `main`. Permissions: `id-token: write` (OIDC), `packages: write`.

| Job | Depends on | Does |
|---|---|---|
| `test` | — | `uv sync && uv run pytest` |
| `build` | `test` | docker build; push `ghcr.io/<owner>/job-agent:<sha>` (public image) |
| `deploy` | `build` | `az login` via OIDC federated credential; `az deployment group create` with `imageTag=<sha>` |

`needs:` chaining is the no-override deploy gate (FR-008/-009): the workflow has
no `workflow_dispatch` on build/deploy, no `continue-on-error`, and no path to the
deploy job that does not pass through `test`. A failed workflow notifies the
maintainer through GitHub's standard run-failure notification — a channel distinct
from the overnight alert path (spec edge case "deployment fails after tests
pass"); the previously deployed image keeps running.

Secret hygiene (FR-011): app secrets live only in Key Vault and never transit CI;
OIDC means no stored cloud credential. The only CI-held secrets are the alert
receivers (`ALERT_EMAIL`, `SMS_COUNTRY_CODE`, `SMS_PHONE`), masked by GitHub
Actions. "Secret value" means: API keys, SMTP credentials, OIDC tokens, and Key
Vault secret *values* (secret *names* are public contract). The workflow must not
echo parameter values or run `az` with `--debug`.

Whole chain must fit SC-003's 15-minute budget.

## On-demand trigger (FR-019)

```bash
az containerapp job start -n <job> -g <rg>                       # respects skip-if-succeeded
az containerapp job start -n <job> -g <rg> \
  --env-vars JOBAGENT_FORCE=1                                    # force full re-run
az containerapp job start -n <job> -g <rg> \
  --env-vars JOBAGENT_FORCE=1 JOBAGENT_MAX_POSTINGS_PER_RUN=1000 # first-run backlog backfill
```

RBAC-gated by `microsoft.app/jobs/start/action` — held only by the maintainer per
the identity matrix above (FR-019, FR-022). Manual starts respect the in-flight
lock: if a run for the current `digest_date` is executing, the new execution
no-ops (FR-017; `JOBAGENT_FORCE` does **not** bypass it — see
[runtime-config.md](runtime-config.md) and data-model.md's startup check).
