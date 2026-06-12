# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once it reaches 1.0.0. Before 1.0.0, minor versions may include breaking changes.

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
