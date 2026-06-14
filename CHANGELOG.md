# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once it reaches 1.0.0. Before 1.0.0, minor versions may include breaking changes.

## [0.2.0] - 2026-06-13

### Added

- Continuous deployment: a push to `main` now runs the test suite and, only
  on green, builds the container image and redeploys to Azure with no manual
  step (`.github/workflows/deploy.yml`). Auth is via OIDC, with no cloud
  credential stored in the repo. The previous manual deploy path
  (`docs/manual-deployment.md`) remains the fallback for initial bootstrap
  and disaster recovery; see `docs/ci-cd.md` for one-time activation and the
  validation drill.
- Missed-deadline alerting: if the daily digest hasn't run successfully by
  the configured local deadline (default 06:00, `deliveryDeadlineHourLocal`),
  an Azure scheduled query alert pages the maintainer by email and SMS within
  30 minutes, and self-clears once a run succeeds. Receivers are supplied at
  deploy time via required, never-committed parameters (`alertEmail`,
  `smsCountryCode`, `smsPhone`), wired through from repo secrets by the new
  deploy workflow.
- Run-failure visibility: a fetch source that fails (adapter error or an
  unknown ATS vendor in `registry.txt`) no longer disappears into the logs —
  the run continues with the remaining sources, and the digest email gets a
  notice naming the affected source(s) without leaking error details. If
  scoring leaves postings unscored (per-run cap or LLM unavailability), the
  digest notes the backlog size and the postings stay queued for the next
  run. Such a run is recorded with outcome `degraded` and a human-readable
  `detail` summary on the `runs` table, while still printing `RUN_SUCCESS` so
  the missed-deadline alert does not fire on a run that did deliver a digest.

### Changed

- `pyyaml` is now a project dependency.

### Removed

- The stale compiled `infra/main.json` (a leftover bicepparam artifact
  referencing the removed `JOBAGENT_MAX_LLM_CALLS` var); `.gitignore` now
  guards against it being regenerated and committed.

## [0.1.1] - 2026-06-12

### Added

- Deterministic pre-LLM relevance filter (`src/job_agent/filter.py`): a
  function-title denylist (hard reject), an advisory allowlist, a
  posting-age gate, and a region/metro location gate drop obviously
  irrelevant postings before any LLM call. Rejections persist with a
  machine-readable `filter_reason` and are never re-scored. Configured by a
  new required runtime file, `filter.toml` (git-ignored, personal); a
  generic `filter.toml.example` is committed as the template. The score
  stage fails loud (`sys.exit`) if `filter.toml` is missing or malformed.
- Per-run budget guardrails for the score stage: `JOBAGENT_MAX_POSTINGS_PER_RUN`
  (default 200) and `JOBAGENT_MAX_COST_PER_RUN` (default 5.00 USD estimated).
  The run stops cleanly at whichever cap is hit first, logs
  `SCORE_CAP_STOP`, exits 0, and the next run resumes the remainder.
- Prompt caching: the screening prompt, candidate profile, salary-floor
  rule, and output-format instructions are now sent as a single cached
  ephemeral `system` block, so the static prefix is billed once per run
  instead of once per batch.
- Cost observability: one `SCORE_SUMMARY` log line per run reports the
  fetched/filtered/scored/remaining counts, a per-reason filter breakdown,
  the four token-usage totals (input, output, cache-write, cache-read), and
  `est_cost_usd`. New per-MTok price env vars `JOBAGENT_PRICE_INPUT`,
  `JOBAGENT_PRICE_OUTPUT`, `JOBAGENT_PRICE_CACHE_WRITE`, and
  `JOBAGENT_PRICE_CACHE_READ` (defaults: 3 / 15 / 3.75 / 0.30, the
  `claude-sonnet-4-6` rates) feed both the cost cap and the estimate.
- `store.py`: new `filter_reason` column on `postings` (idempotent
  migration for existing databases), plus `scorable()` and
  `record_filter_rejections()` helpers.
- `infra/main.bicep` / `infra/main.bicepparam`: declare the new per-run cap
  and per-MTok price env vars for the Container Apps Job.

### Removed

- The superseded, never-implemented `JOBAGENT_MAX_LLM_CALLS` env var.

## [0.1.0] - 2026-06-11

### Added

- Initial fetch -> score -> digest pipeline: `fetch.py` reads `registry.txt`
  and dispatches per-vendor adapters (`greenhouse`, `lever`) into a shared
  `Posting` schema; `score.py` batches unscored postings against
  `profile.md` + `screening_prompt.md` via the Anthropic API; `digest.py`
  emails a triaged HTML/text digest over SMTP and marks postings as sent.
- SQLite persistence (`store.py`): `postings` and `applications` tables,
  idempotent `INSERT OR IGNORE` upserts so re-runs never clobber scores or
  re-surface dismissed roles.
- `main.py` as the container/scheduled-job entry point, running the full
  pipeline in one process with a run-lifecycle wrapper: a `runs` table
  records each day's attempts, `startup_decision()` skips an in-flight or
  already-succeeded run (bypassable with `JOBAGENT_FORCE=1`), and the
  process prints `RUN_SUCCESS` / `RUN_FAILED_FINAL` markers and exits
  non-zero on fatal failure (one run + up to 2 retries per day).
- Empty-day "no new matches" digest notice, so the presence of an email
  always confirms the pipeline ran.
- Revised four-part dedupe fingerprint
  (`sha256(title|company|location|description)[:16]`), with an automatic,
  idempotent re-key migration that preserves existing scores, application
  status, and `digest_sent_at` for previously-stored postings.
- `JOBAGENT_DATA_DIR` env var: relocates `jobs.db`, `registry.txt`,
  `profile.md`, and `screening_prompt.md` under a single data directory
  (e.g. an Azure Files mount), defaulting to the current directory for local
  development.
- Default scoring model `claude-sonnet-4-6` (overridable via
  `JOBAGENT_MODEL`).
- `Dockerfile` and `.dockerignore` for a runtime container image built on
  `uv sync --frozen`; personal/runtime files (`profile.md`,
  `screening_prompt.md`, `registry.txt`, `jobs.db`) are excluded from the
  image and supplied at runtime via a mounted volume.
- Azure infrastructure as code (`infra/main.bicep`,
  `infra/main.bicepparam`): Container Apps scheduled Job, Key Vault with
  seven secrets (including `salary-floor`), Log Analytics, and an
  Azure Files share mounted at `/data` for persistent state.
- `scripts/bootstrap.sh` for idempotent one-time setup of the deployment
  identity and resource group role assignments.
- `docs/manual-deployment.md`: a manual end-to-end deployment walkthrough
  (bootstrap, image push, two-pass Bicep deploy, Key Vault secrets, data
  upload, smoke run, force-run semantics).

### Known limitations

- No CI/CD: deploys are manual, per `docs/manual-deployment.md`. Automatic
  test-then-deploy on push to `main` is planned (US2).
- No platform-level alerting yet: a missing digest email is the only
  failure signal. Alert email/SMS on run failure is planned (US3).
