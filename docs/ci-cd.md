# Continuous Deployment (Feature 001, US2)

Once activated, **every push to `main` runs the test suite and — only on green —
builds the image and deploys to Azure, with no manual step**. This replaces
steps 2–3 of [manual-deployment.md](manual-deployment.md); the manual path stays
the fallback for the initial bootstrap and for disaster rebuilds (US5).

## What the workflow does

[`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml), on push to `main`:

| Job | Does |
|---|---|
| `test` | `uv sync` → `uv run pytest` |
| `build` (needs `test`) | docker build; push `ghcr.io/<owner>/job-agent:<sha>` |
| `deploy` (needs `build`) | `az login` via OIDC; `az deployment group create … imageTag=<sha>` plus the alert receivers from repo secrets |

The `needs:` chain is the no-override deploy gate (FR-008/-009): no
`workflow_dispatch` on build/deploy, no `continue-on-error`, no path to deploy
that skips `test`. A failed run is surfaced by GitHub's own run-failure
notification, and the previously deployed image keeps serving. Auth is OIDC — no
cloud credential is stored (FR-011).

## One-time activation (maintainer)

**Prerequisite:** [`scripts/bootstrap.sh`](../scripts/bootstrap.sh) has run — it
creates the deploy identity, its federated credential (trusting
`repo:<owner>/job-agent:ref:refs/heads/main`), and the RG-scoped roles the deploy
needs.

1. Get the deploy identity's client ID (an identifier, not a secret):

   ```bash
   az identity show -g jobagent-rg -n jobagent-deploy --query clientId -o tsv
   ```

   (It is also emitted as the `deployIdentityClientId` deployment output.)

2. Set the repo secrets + variable the workflow reads (values typed directly,
   never committed):

   ```bash
   gh secret   set AZURE_CLIENT_ID       --body "<deploy-identity-client-id>"
   gh secret   set AZURE_TENANT_ID       --body "<tenant-id>"
   gh secret   set AZURE_SUBSCRIPTION_ID --body "<subscription-id>"
   gh secret   set ALERT_EMAIL           --body "<alert email address>"
   gh secret   set SMS_COUNTRY_CODE      --body "<e.g. 1>"
   gh secret   set SMS_PHONE             --body "<phone digits only>"
   gh variable set AZURE_RESOURCE_GROUP  --body "jobagent-rg"
   ```

   `ALERT_EMAIL` / `SMS_COUNTRY_CODE` / `SMS_PHONE` are personal data and are
   **required** on every deploy (the Bicep params have no defaults) — the
   workflow passes them to `infra/main.bicep` so the action-group receivers are
   restored on each redeploy without ever being committed.

3. After the **first** successful `build`, make the GHCR package **public** so
   the Container Apps job can pull it without registry credentials: GitHub →
   Packages → `job-agent` → Package settings → Change visibility → Public. (A
   package created by the first push defaults to private.)

## Validation drill (T027 — requires Azure + GitHub)

1. **Green path (SC-003, FR-009):** push a trivially observable change to `main`.
   Confirm `test` ran first, then `build` + `deploy`, the change is live within
   ~15 minutes, and no human action was needed after the push.
2. **Red path (FR-008):** push a commit with a deliberately failing test.
   Confirm `test` fails, `build`/`deploy` are skipped, and the previously
   deployed image keeps serving.
3. **Secret hygiene (FR-011):** open the Actions logs and confirm no secret
   values appear (no API keys, SMTP creds, OIDC tokens, or Key Vault secret
   *values*; `az` was not run with `--debug`). Secret *names* are public contract.

## Alert receivers (wired)

US3 alerting landed in `infra/main.bicep` (T036), so `deploy.yml` now exposes the
action-group receivers (`ALERT_EMAIL` / `SMS_COUNTRY_CODE` / `SMS_PHONE`) as
environment variables on the deploy step, and `infra/main.bicepparam` reads them
(`readEnvironmentVariable`) into the Bicep params `alertEmail` / `smsCountryCode`
/ `smsPhone` from repo secrets on every deploy. They are required-with-no-default,
so a deploy missing any of them fails loud rather than building a receiver-less
alert. Set all three in activation step 2 above before the first CI deploy.
