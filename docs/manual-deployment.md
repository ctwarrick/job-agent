# Manual MVP Deployment

This walkthrough is the **interim, pre-CI** path to a working overnight digest:
bootstrap → build and push the image by hand → deploy `infra/main.bicep` →
set secrets → upload runtime files → trigger a smoke run.

**MVP honesty check**: at this stage there is no GitHub Actions workflow yet
(US2) — you build, push, and deploy by hand. Platform alerting (US3) *is* now
part of `infra/main.bicep`: deploying in step 3 with the receiver parameters
creates the action group + missed-deadline alert rule, so a silently failed
night pages you (email + SMS) by ~06:30. Confirm it end-to-end with the
**required** US3 alert drill in
[`quickstart.md`](../specs/001-azure-deployment/quickstart.md) §US3 (needs Azure
access) before relying on it; until then, also watch
`az containerapp job logs show` after any change.

For background, see:

- [`specs/001-azure-deployment/contracts/deployment.md`](../specs/001-azure-deployment/contracts/deployment.md)
  — resource inventory, parameters, identity matrix
- [`specs/001-azure-deployment/contracts/runtime-config.md`](../specs/001-azure-deployment/contracts/runtime-config.md)
  — env vars, exit codes, log markers
- [`specs/001-azure-deployment/quickstart.md`](../specs/001-azure-deployment/quickstart.md)
  — full validation walkthrough across all user stories

## Prerequisites

- Azure subscription + tenant ID (yours — never committed)
- `az` CLI logged in as the maintainer (`az login`), with the Bicep CLI
  available (`az bicep install` if needed)
- Docker, to build the image locally
- Local copies of `profile.md`, `screening_prompt.md`, `registry.txt`, and
  `filter.toml` (git-ignored, never referenced by content below — file names
  only; `filter.toml` starts as a copy of the committed `filter.toml.example`)
- Anthropic API key and SMTP credentials at hand (values are typed directly
  into `az keyvault secret set` commands, never written to disk or committed)

## 1. Bootstrap the resource group and deploy identity

```bash
AZURE_SUBSCRIPTION_ID=<your-subscription-id> \
AZURE_TENANT_ID=<your-tenant-id> \
GITHUB_REPO=<your-github-username>/job-agent \
RESOURCE_GROUP=jobagent-rg \
LOCATION=westus2 \
  ./scripts/bootstrap.sh
```

This creates the resource group and a user-assigned managed identity for
future GitHub-OIDC deploys (US2). It is safe to re-run.

## 2. Build and push the image to ghcr.io (manual, interim)

Until US2's CI pipeline lands, build and push the image yourself:

```bash
docker build -t ghcr.io/<your-github-username>/job-agent:manual .

echo "$GHCR_PAT" | docker login ghcr.io -u <your-github-username> --password-stdin
docker push ghcr.io/<your-github-username>/job-agent:manual
```

Make the package public (or grant the Container Apps environment pull access)
so the job can pull it. Note the tag (`manual` here) — it is the `imageTag`
parameter in the next step.

## 3. Deploy the infrastructure (first pass — expected to fail at the job)

```bash
export ALERT_EMAIL='<your alert email>'
export SMS_COUNTRY_CODE='<e.g. 1>'
export SMS_PHONE='<your phone, digits only>'

az deployment group create \
  --resource-group jobagent-rg \
  --template-file infra/main.bicep \
  --parameters infra/main.bicepparam \
  --parameters imageTag=manual imageRepository=ghcr.io/<your-github-username>/job-agent
```

`alertEmail`, `smsCountryCode`, and `smsPhone` are **required** and have no
defaults (a forgotten value fails the deploy rather than building a
receiver-less alert). They are personal data: `infra/main.bicepparam` reads them
from the environment (`readEnvironmentVariable`) at compile time, so export the
three variables above before deploying — never put the values in
`infra/main.bicepparam`
([deployment.md](../specs/001-azure-deployment/contracts/deployment.md) →
Never-committed parameters).

This creates the Log Analytics workspace, Container Apps environment, storage
account + Azure Files share, Key Vault, the job's user-assigned managed
identity (granted *Key Vault Secrets User* on the vault), and the Container
Apps Job.

**Important**: on this first pass, the job's container definition references
seven Key Vault secrets (`anthropic-api-key`, `smtp-host`, `smtp-port`,
`smtp-user`, `smtp-pass`, `digest-to`, `salary-floor`) that do not exist yet,
so the job resource is expected to **fail to deploy** — every other resource
(Log Analytics, storage, Key Vault, the managed identity and its role
assignment, the Container Apps environment, the action group, and the
missed-deadline alert rule) deploys successfully. Because the deployment as a
whole failed, it returns **no outputs**.

Get the names of the resources that did deploy directly, rather than from
deployment outputs:

```bash
RG=jobagent-rg

KV=$(az keyvault list -g "$RG" --query '[0].name' -o tsv)
STORAGE_ACCOUNT=$(az storage account list -g "$RG" --query '[0].name' -o tsv)
```

Continue to step 4 to set the secrets, then **re-run the same
`az deployment group create` command** from this step so the job picks up the
now-existing secrets and converges.

## 4. Set the seven Key Vault secrets

```bash
az keyvault secret set --vault-name "$KV" --name anthropic-api-key --value "<your Anthropic API key>"
az keyvault secret set --vault-name "$KV" --name smtp-host          --value "<smtp host>"
az keyvault secret set --vault-name "$KV" --name smtp-port          --value "<smtp port>"
az keyvault secret set --vault-name "$KV" --name smtp-user          --value "<smtp user>"
az keyvault secret set --vault-name "$KV" --name smtp-pass          --value "<smtp password>"
az keyvault secret set --vault-name "$KV" --name digest-to          --value "<recipient email address>"
az keyvault secret set --vault-name "$KV" --name salary-floor       --value "<base salary floor in dollars, e.g. 120000>"
```

These values are never written to the repo, this doc, or any log. After
setting them, re-run the `az deployment group create` command from step 3 so
the job's secret references resolve and the deployment converges (this second
pass returns the full deployment outputs, including `storageAccountName` and
`fileShareName` used below).

## 5. Upload the runtime files

The job reads `profile.md`, `screening_prompt.md`, `registry.txt`, and
`filter.toml` from the Azure Files share at `/data` (`JOBAGENT_DATA_DIR=/data`).
Upload your local, git-ignored copies by **file name only** — never paste their
contents into a shell command, this doc, or any agent transcript:

```bash
SHARE=<file-share-name-from-output>

az storage file upload --account-name "$STORAGE_ACCOUNT" --share-name "$SHARE" --source profile.md
az storage file upload --account-name "$STORAGE_ACCOUNT" --share-name "$SHARE" --source screening_prompt.md
az storage file upload --account-name "$STORAGE_ACCOUNT" --share-name "$SHARE" --source registry.txt
az storage file upload --account-name "$STORAGE_ACCOUNT" --share-name "$SHARE" --source filter.toml
```

The score stage fails loud (`sys.exit`) if `filter.toml` is missing or
malformed, so this upload is required, not optional.

This is the **single sanctioned recurring manual operation** in production —
re-run it whenever any of these four files changes (US4).

## 6. Manual smoke run

Confirm the pipeline runs end-to-end on the platform, including outbound SMTP,
and seed the first `RUN_SUCCESS` marker:

```bash
JOB=<job-name-from-output>
RG=jobagent-rg

az containerapp job start -n "$JOB" -g "$RG"
```

No `--env-vars` override (and no `JOBAGENT_FORCE=1`) is needed for this first
run: `az containerapp job start --env-vars ...` does not *merge* env vars —
it replaces the entire execution template with a single container that has
only the supplied env vars, no image, no secret-backed env vars, and no
`/data` volume mount, so the run is rejected or executes a stripped
container. On a fresh share the `runs` table is empty, so the
"already succeeded today" check has nothing to skip and the plain `start`
proceeds normally.

Then:

- Check the inbox at `digest-to` for the digest (or "no new matches" notice).
- Check logs for `RUN_SUCCESS digest_date=<today>`:

  ```bash
  az containerapp job logs show -n "$JOB" -g "$RG" --container job-agent
  ```

  `--container job-agent` is required — it names the container in the job's
  template (the `job-agent` container declared in `infra/main.bicep`), not the
  job. Alternatively, query `ContainerAppConsoleLogs_CL` in the Log Analytics
  workspace. (Log Analytics ingests with a few-minutes lag, so logs may be
  briefly empty right after a run.)

If this run fails, diagnose from the logs (per-source fetch failures, the
failing stage, and the exception are all logged per
[runtime-config.md](../specs/001-azure-deployment/contracts/runtime-config.md)),
fix the issue, and re-run step 6 with the same plain `az containerapp job
start -n "$JOB" -g "$RG"` — a failed run does not set `RUN_SUCCESS`, so the
"already succeeded today" skip still has nothing to bypass.

### Forcing a re-run after a same-day success

If you need to manually re-run the pipeline *after* a `RUN_SUCCESS` has
already been recorded for today (e.g. to test a fix), `JOBAGENT_FORCE=1`
bypasses that skip — but it must be set via a template update, not via
`az containerapp job start --env-vars ...`, which (as above) replaces the
whole container template and drops the image, secrets, and volume mount.
Use the merge-safe update/start/cleanup sequence instead:

```bash
az containerapp job update -n "$JOB" -g "$RG" --set-env-vars JOBAGENT_FORCE=1
az containerapp job start -n "$JOB" -g "$RG"
az containerapp job update -n "$JOB" -g "$RG" --remove-env-vars JOBAGENT_FORCE
```

The final `update` clears `JOBAGENT_FORCE` from the job's template so it
doesn't silently bypass the skip on the next scheduled run. `JOBAGENT_FORCE=1`
bypasses the "already succeeded today" skip but not the in-flight lock.

## What's next

- **US2 is implemented** — [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml)
  replaces steps 2–3's manual build/push/deploy with a `test → build → deploy`
  GitHub Actions workflow on every push to `main`. Activate it (one-time repo
  secrets + the validation drill) per [docs/ci-cd.md](ci-cd.md). Until you
  activate it, this manual path remains the steady state.
- **US3 alerting is implemented** — `infra/main.bicep` now contains the action
  group + scheduled query alert rule, so a missed morning digest pages the
  maintainer (email + SMS) by ~06:30 instead of being noticed (or not) the next
  time someone checks email. It deploys with step 3; the **required** alert
  drill (quickstart §US3) is the remaining validation and needs Azure access.

Until US2 is activated, treat this manual path as the steady state: redeploy by
repeating steps 2–3 (rebuild/push a new tag, redeploy with the new `imageTag`),
and watch the inbox each morning.
