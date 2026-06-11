# Phase 0 Research: Cloud-Native Scheduled Operation on Azure

**Feature**: `001-azure-deployment` | **Date**: 2026-06-11
**Input**: [spec.md](spec.md), constitution v1.0.0, codebase recon (handoff 2026-06-11)

All "deliberately deferred to planning" items from the spec (storage service, email
mechanism, infrastructure tooling) are resolved here, along with every technology
choice the plan depends on. Azure facts marked ✅ were verified against Microsoft
Learn / the `Microsoft.App/jobs` ARM schema on 2026-06-11.

---

## 1. Compute + scheduler: Azure Container Apps Job (Schedule trigger)

- **Decision**: Run `main.py` as an Azure Container Apps **Job** with trigger type
  `Schedule`, on a Consumption-plan environment. Job settings: `parallelism: 1`,
  `replicaCompletionCount: 1`, `replicaRetryLimit: 0` (retry handled by the
  cron-tick design in §2), `replicaTimeout` set below the attempt spacing
  (e.g. 15 minutes).
- **Rationale**: The constitution requires the same `main.py` code path locally and
  in production (Principle II) and scale-to-zero consumption compute (Principle I).
  A containerized job runs the identical entry point with zero application
  restructuring. Consumption-plan jobs bill per vCPU-second/GiB-second with a
  monthly free grant (180K vCPU-s, 360K GiB-s) — three short runs a day lands at
  ~$0. Built-in cron trigger removes the need for a separate scheduler resource.
- **Alternatives considered**:
  - *Azure Functions (timer trigger)* — rejected: requires restructuring the
    pipeline into a function app (new dependency, divergent code path), and
    `WEBSITE_TIME_ZONE` is unsupported on Linux consumption plans, so it has the
    same UTC-cron limitation anyway.
  - *Logic Apps + Azure Container Instances* — rejected: two resources where one
    suffices (Principle IV), and ACI has no scheduler of its own.

## 2. Schedule, DST, and retry (FR-002, FR-018): multi-tick UTC cron + idempotent runs

- **Decision**: One cron expression with three firings ~20 minutes apart, default
  `0,20,40 11 * * *` (11:00 / 11:20 / 11:40 UTC = 03:xx PST / 04:xx PDT). Each tick
  is a full job execution; the app checks a new `runs` table at startup and exits 0
  (no-op) if the current digest date already has a successful run. The digest date
  is computed timezone-aware via `JOBAGENT_TZ` (default `America/Los_Angeles`,
  stdlib `zoneinfo`). The cron expression and timezone are Bicep/env parameters —
  configuration, not code (FR-002).
- **Rationale**: ✅ Verified: ACA job cron is **UTC-only** (MS Learn "Jobs in Azure
  Container Apps": "Cron expressions in scheduled jobs are evaluated in Coordinated
  Universal Time (UTC)"; also microsoft/azure-container-apps#1109), and
  `replicaRetryLimit` retries immediately with no 15–30-minute spacing. Three
  spaced ticks + idempotent skip implement exactly "up to 2 automatic retries,
  ~15–30 minutes apart" (FR-018): tick 1 is the run; ticks 2–3 only execute work
  when an earlier tick failed. Scheduling the *first* attempt at 11:00 UTC means
  even the *last* attempt (11:40 UTC) completes hours before the 06:00
  America/Los_Angeles deadline in **both** DST phases — the deadline tracks the
  configured timezone (spec edge case) by construction, with margin absorbing the
  UTC offset shift. A timezone or deadline change = recompute the cron parameter
  and redeploy; the formula is documented in `contracts/deployment.md`.
- **Alternatives considered**:
  - *Logic App recurrence trigger (native timezone/DST support) starting the job
    via ARM* — rejected: an extra always-present resource and second trigger
    mechanism (Principle IV) to solve a problem margin already solves.
  - *`replicaRetryLimit: 2`* — rejected: retries are immediate, violating the
    clarified 15–30-minute spacing meant to let transient outages clear.

## 3. Storage (FR-004) + runtime files (FR-012): Azure Files share, SQLite unchanged

- **Decision**: One Azure **Files** SMB share, mounted into the job container via
  Container Apps environment storage. Both the SQLite database (`jobs.db`) and the
  personal runtime files (`profile.md`, `screening_prompt.md`, `registry.txt`) live
  on the share. App change: a single `JOBAGENT_DATA_DIR` env var (default `.`)
  prefixes the DB path and runtime-file paths — local development is unchanged.
  Runtime-file updates in production are a documented
  `az storage file upload` invocation (auth-gated by Azure RBAC + account key held
  only by the maintainer) — the constitution's single sanctioned manual operation.
  The storage account allows no public access.
- **Rationale**: ✅ Verified: the `Microsoft.App/jobs` ARM/Bicep schema supports
  `volumes` with `storageType: 'AzureFile'` plus container `volumeMounts` (jobs
  share the environment-storage mechanism with apps). Keeping SQLite preserves
  `store.py` and the local/prod single code path (Principles II, IV); sent-state
  (`digest_sent_at`) is durably on the share the moment it is written, so a crash
  after the digest is emailed can never cause a duplicate digest the next day
  (FR-004). SQLite-over-SMB locking is the known risk; it is acceptable here
  because FR-017 guarantees exactly one writer at a time. Cost: a few MB on a
  transaction-optimized share ≈ <$1/month.
- **Alternatives considered**:
  - *Blob download/upload wrapper around a local SQLite file* — rejected: a crash
    between email-send and re-upload loses `digest_sent_at` and re-sends the same
    digest; the wrapper is also new failure-prone code.
  - *Table Storage / Cosmos DB serverless / Azure SQL serverless* — rejected:
    rewrite of `store.py` (divergent local/prod paths), and Azure SQL serverless
    carries a ~$5+/month floor plus auto-pause cold starts.

## 4. Infrastructure as code (FR-010): Bicep

- **Decision**: Define all resources in **Bicep** under `infra/`, deployed with
  `az deployment group create` (idempotent). A small documented bootstrap script
  creates the resource group and the GitHub-OIDC deployment identity (chicken-egg
  with CI); everything else lives in `main.bicep` + parameter file.
- **Rationale**: Bicep needs no state backend (ARM is the state) — Terraform would
  require a storage account *for state* before any state exists, a second
  bootstrap problem and an extra toolchain. Re-applying the deployment restores
  declared state, satisfying US5's drift scenario. Az CLI is already the
  deployment tool in CI. Serves the constitution's stated IaC-learning goal on the
  platform-native language.
- **Alternatives considered**: *Terraform* — rejected (state backend = extra
  component, Principle IV). *Pulumi* — rejected (heavier runtime dependency).

## 5. Container registry: GitHub Container Registry (ghcr.io), public image

- **Decision**: CI builds the image and pushes to **ghcr.io** under the repo's
  namespace as a public image; the job pulls it anonymously.
- **Rationale**: $0 and native to GitHub Actions. The image contains only the
  public repo's code plus public dependencies — runtime files, secrets, and the
  database are injected at runtime and never baked in (Principle VI), so a public
  image leaks nothing the public repo doesn't already publish.
- **Alternatives considered**: *Azure Container Registry Basic* — rejected:
  ~$5/month ≈ 10% of the total ceiling for zero added benefit here. *ghcr private
  image* — noted as a trivial follow-up if ever wanted (add a registry credential
  secret); not needed now.

## 6. Digest email delivery: existing SMTP path, unchanged

- **Decision**: Keep `digest.py`'s SMTP delivery exactly as-is; production supplies
  `SMTP_HOST/PORT/USER/PASS` and `DIGEST_TO` via the secret store (§7).
- **Rationale**: The spec's assumption is that email delivery already works locally
  and only *where it runs* changes. Zero code change, zero new dependency.
- **Alternatives considered**: *Azure Communication Services Email* — rejected:
  new resource + SDK dependency + sender-domain setup for no requirement it
  satisfies better. Revisit only if the SMTP provider blocks cloud egress in
  practice (a validation step in quickstart.md checks this).

## 7. Secrets (FR-011): Key Vault + managed identity references

- **Decision**: An Azure **Key Vault** holds `ANTHROPIC_API_KEY`, SMTP credentials,
  and recipient address. The job's system-assigned managed identity gets the *Key
  Vault Secrets User* role; the job's ACA secrets are declared as `keyVaultUrl`
  references and surfaced to the container as env vars via `secretRef`. Secret
  *values* are set once by the maintainer with `az keyvault secret set`
  (documented bootstrap step) and never transit the repository or CI.
- **Rationale**: ✅ Verified: ACA **job** `configuration.secrets` supports
  `keyVaultUrl` + `identity` (ARM schema: "Resource ID of a managed identity to
  authenticate with Azure Key Vault, or System"). Key Vault keeps secret values
  out of every deploy path: CI deploys infrastructure without ever holding
  application secrets, which is the cleanest reading of FR-011/SC "no secret value
  in CI logs". Key Vault standard tier cost at this volume rounds to $0.
- **Alternatives considered**: *ACA native secrets fed from GitHub secrets on each
  deploy* — workable (GitHub masks log values) but rejected: secret values then
  live in two stores and flow through every CI run; rotating means touching CI.

## 8. Failure alerting (FR-006, SC-004): absence-of-success log alert → email + SMS

- **Decision**: The app logs a structured marker line `RUN_SUCCESS digest_date=<d>`
  on every successful run (digest or no-matches day). A Log Analytics **scheduled
  query alert** queries `ContainerAppConsoleLogs_CL` for that marker (≈25-hour
  lookback, evaluated every 30 minutes); if none is found, it fires an Azure
  Monitor **action group** with an email receiver and an SMS receiver — delivery
  paths fully independent of the app's SMTP. A second, optional fast-path rule on
  a `RUN_FAILED_FINAL` marker is noted but not required for the MVP.
- **Rationale**: Absence-of-success is strictly stronger than presence-of-failure:
  it also catches "the scheduler never started the container" and "the container
  hung", which no failure-event alert can see. It naturally implements "alert only
  after the final retry fails" (FR-018) — ticks 1–2 failing doesn't fire anything
  as long as a later tick succeeds within the lookback. Known cosmetic caveat:
  the first deployment day alerts until the first successful run; documented in
  quickstart. ✅ Pricing verified: log search alert rules at ≥5-minute frequency
  bill ~$0.50/month for the first time series; action-group emails are free at
  this volume and SMS is per-message on rare failure nights (≈$0).
- **Alternatives considered**:
  - *Metric alert on failed job executions* — rejected: fires on attempt 1,
    violating the clarified "alert only if the final attempt fails", and misses
    never-started runs.
  - *healthchecks.io dead-man's switch* — rejected: external dependency outside
    the IaC story and another account to operate.

## 9. CI/CD (FR-008, FR-009): GitHub Actions, OIDC, test-gated deploy

- **Decision**: `.github/workflows/deploy.yml` on push to `main`:
  job 1 `uv sync && uv run pytest` → job 2 (needs 1) docker build + push to ghcr
  → job 3 (needs 2) `az login` via **OIDC federated credential** (user-assigned
  managed identity; no stored cloud secret), `az deployment group create` (Bicep),
  then update the job's image tag. A failing suite stops the chain with no
  override (`needs:` is the gate).
- **Rationale**: Direct implementation of Principle III and FR-008/-009; OIDC
  removes the one long-lived cloud credential GitHub would otherwise hold. The
  whole chain comfortably fits SC-003's 15-minute budget (suite is seconds; image
  is small).
- **Alternatives considered**: *Azure DevOps* — rejected: repo, reviews, and
  gating already live on GitHub. *Service-principal secret auth* — rejected:
  long-lived secret where a federated credential works.

## 10. Spend visibility (FR-014): Azure budget in Bicep + Anthropic console limit

- **Decision**: A `Microsoft.Consumption/budgets` resource ($50/month, alert
  thresholds at 50% and 80%, notifying the maintainer's email) declared in Bicep.
  On the Anthropic side — invisible to Azure — the maintainer sets a Console spend
  limit/notification as a documented bootstrap step (suggested ~$25/month,
  final value is the maintainer's call). The per-run LLM bound (§12) caps the
  blast radius of any single defective night.
- **Rationale**: Constitution Principle I explicitly requires watching **both**
  bills. Budgets are free, declarable in Bicep, and alert-only (the spec rules out
  auto-halt).
- **Alternatives considered**: *Cost anomaly alerts* — additive, not declarable as
  simply; can be added later without design change.

## 11. LLM model + cost (Principle I gate)

- **Decision**: Bump the in-code default model from `claude-opus-4-20250514`
  (⚠️ **deprecated, retires 2026-06-15 — four days after this plan**) to
  **`claude-sonnet-4-6`**, and set `JOBAGENT_MODEL=claude-sonnet-4-6` explicitly in
  production config. Model remains env-selectable per the constitution
  (`claude-haiku-4-5` is the cheaper dial-down).
- **Rationale**: ✅ Verified against the Claude API reference (2026-06-11): the
  current default would 404 mid-month, breaking the overnight run — fixing it is a
  cloud-operation necessity, not scope creep. Sonnet 4.6 ($3 in / $15 out per
  MTok) is the cost-discipline default for scoring job postings; Opus-tier
  ($5/$25) is not justified for this task (Principle I requires considering the
  cheaper alternative — and choosing it here).
- **Estimated monthly cost** (the constitution requires this in every plan):

  | Item | Estimate |
  |---|---|
  | Container Apps Job (3 short runs/day, consumption) | ~$0 (within free grant) |
  | Azure Files share (few MB + daily IO) | <$1 |
  | Log Analytics ingestion (console logs, single daily job) | <$1 |
  | Scheduled query alert rule (30-min frequency) | ~$0.50–1.50 |
  | Action group (email free; SMS only on failure nights) | ~$0 |
  | Key Vault (standard, a handful of reads/day) | ~$0 |
  | Consumption budget, RBAC, identities, ghcr, GitHub Actions (public repo) | $0 |
  | **Azure subtotal** | **≈ $2–4/month** |
  | Anthropic API (≈30 new postings/day, batches of 6, ~10K in / 2K out per call, Sonnet 4.6) | ≈ $2–10/month |
  | **All-in** | **≈ $5–15/month — well under the $50 ceiling** ✓ |

- **Alternatives considered**: `claude-haiku-4-5` ($1/$5) — kept available via
  env; `claude-opus-4-8` ($5/$25, the official drop-in for the retiring model) —
  rejected as default on cost.

## 12. Application-side changes (minimal set, per spec assumptions)

- **Decision**: Limit code changes to the cloud-operation needs the spec already
  enumerates:
  1. `JOBAGENT_DATA_DIR` prefix for DB + runtime-file paths (§3).
  2. `runs` table + timezone-aware digest date + skip-if-already-succeeded (§2),
     plus `RUN_SUCCESS` / `RUN_FAILED_FINAL` structured log markers (§8).
  3. Empty-day notice email — `digest.py` currently prints and returns without
     sending when no rows qualify; FR-003 requires an email every successful run.
  4. Per-source fetch failure capture — `fetch.py` currently only prints to
     stderr; failures must be recorded so the digest can report degraded sources
     (FR-005).
  5. `JOBAGENT_MAX_LLM_CALLS` — cap on scoring batches per run (FR-013).
  6. Retention purge stage — delete postings (and their application rows) with
     status `new`/`dismissed` whose `fetched_at` is older than
     `JOBAGENT_RETENTION_DAYS` (default 60) (FR-015).
  7. Default model bump (§11) and a `Dockerfile` (uv-based, runs `python main.py`).
- **Rationale**: Each maps 1:1 to a functional requirement; everything else in the
  pipeline is untouched (spec's Out of Scope).

## 13. On-demand trigger (FR-019): `az containerapp job start`

- **Decision**: The documented manual trigger is
  `az containerapp job start -n <job> -g <rg>`. ✅ Verified: on-demand start works
  for **any** job type including Schedule, and is RBAC-gated
  (`microsoft.app/jobs/start/action`) so only the maintainer's identity can invoke
  it. Manual-run semantics: a manual start runs through the same idempotent path —
  if today's digest already succeeded, it no-ops; passing `JOBAGENT_FORCE=1` (env
  override on the start command) forces a full re-run. *(Default recommendation:
  keep skip semantics; force flag covers the runtime-file-update verification
  scenario. Flagged to the human gate as an open preference.)*
- **Rationale**: Zero extra infrastructure; concurrency protection (FR-017) is
  preserved because parallelism stays 1 and the skip/lock logic is in the app.

---

## Resolved spec deferrals — summary

| Open question (spec/constitution) | Resolution |
|---|---|
| Production storage service | Azure Files share + unchanged SQLite (§3) |
| Email delivery mechanism | Existing SMTP path, creds via Key Vault (§6, §7) |
| Infrastructure tooling | Bicep + az CLI, OIDC from GitHub Actions (§4, §9) |
| Runtime-file private update mechanism | `az storage file upload` to the private share (§3) |
| Failure alert mechanism | Absence-of-success log alert → action group email + SMS (§8) |
| Overnight start time | 11:00/11:20/11:40 UTC cron, deadline-safe in both DST phases (§2) |

## Open questions for the human gate (non-blocking)

1. Manual-run semantics: skip-if-succeeded with `JOBAGENT_FORCE=1` override
   (recommended) vs. manual runs always execute.
2. SMS receiver number/country for the action group — supplied as a deployment
   parameter, never committed.
3. Anthropic Console spend-limit value (suggest $25/month).
