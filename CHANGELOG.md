# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once it reaches 1.0.0. Before 1.0.0, minor versions may include breaking changes.

## [2.1.0] - 2026-06-26

### Added

- Talemetry / TTC-Portals adapter (`src/job_agent/adapters/talemetry.py`):
  a single-target, server-rendered HTML scraper for one careers site fronted
  by the Talemetry / TTC-Portals recruitment-marketing platform, selected
  from `registry.toml` via `vendor = "talemetry"` plus a required `host`
  field. Listing and detail pages follow a `/jobs/{id}-{slug}/` shape; the
  numeric job ID parsed from the URL becomes the posting's `external_id`,
  while cross-run dedupe still uses the existing content-based fingerprint
  (unchanged). The adapter fetches each job's detail page for the
  description, honors the existing `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` cap,
  applies a politeness sleep between requests, retains postings with no
  parseable location (keep-if-any US gate), and emits a distinct warning when
  a fetch parses zero postings (rather than treating it as a hard failure).
  Registered as `"talemetry"` in `fetch.py`'s `ADAPTERS` dispatch table and
  validated by `registry.py` alongside `greenhouse`/`lever`/`workday`/`icims`.
  `registry.toml.example` documents the schema with the placeholder host
  `careers.example.com`.
- `beautifulsoup4` (on the pure-Python `html.parser` backend, no `lxml`): the
  project's first heavy dependency beyond `anthropic`, added because the
  Talemetry adapter is the first source with no JSON API — see
  `specs/005-talemetry-adapter/plan.md` for the Constitution IV justification.

### Known limitations

- The Talemetry adapter ships **inactive**: it is fully implemented and
  covered by stub-based tests, but no live source is wired into
  `registry.toml`, and its CSS selectors and non-US location markers are
  **unconfirmed placeholders**, never verified against live markup. The one
  intended target fronts its careers board with a Cloudflare "managed
  challenge" that a plain HTTP fetch cannot pass, so live recon
  (`specs/005-talemetry-adapter/tasks.md` T017) was skipped. The capability
  is ready to activate once that access problem is solved (e.g. a headless
  browser or an alternate non-gated feed); until then it contributes no
  postings to any digest.

## [2.0.0] - 2026-06-24

Source configuration moves from the line-oriented `registry.txt` (plus a
separate `companies.toml` for display names) to a single validated
`registry.toml`. **Breaking**: a deploy must upload `registry.toml` before
running this version — the loader fails loud if it is absent.

### Added

- `registry.toml`: a validated TOML source registry, loaded by the new
  `src/job_agent/registry.py` (`Source` dataclass + `load_registry`). One
  `[[source]]` table per ATS board carries `vendor` plus the vendor-specific
  fields (`slug` for greenhouse/lever; `tenant`/`site`/`host` for workday;
  `tenant` + optional `host` for icims), an optional `enabled` flag, and an
  optional `name`. Validation is fail-loud before any fetch: unknown vendor,
  missing required field, duplicate `(vendor, slug)`, or an unrecognized key
  each raise a `ValueError` naming the offending source, so a typo can no
  longer silently drop a company. A committed `registry.toml.example`
  documents the schema.
- `name` as the authoritative digest company: when set on a source it feeds
  the dedupe fingerprint (`schema.py`) directly; otherwise company falls back
  to the slug (greenhouse/lever) or the tenant (workday/icims).

### Changed

- `fetch.main()` iterates the validated `Source` records and passes the
  resolved `company` to each adapter. Every adapter's contract becomes
  `fetch(slug, *, company: str | None = None, timeout=20)`; the per-adapter
  `_resolve_company` helpers are gone.

### Removed

- `registry.txt` and its line-oriented `vendor  slug` format, superseded by
  `registry.toml`.
- `companies.toml` and `companies.toml.example`, plus the `_resolve_company`
  display-name lookup — the `name` field on each `registry.toml` source
  replaces the old `[display_names]` table.

## [1.2.0] - 2026-06-22

### Added

- iCIMS adapter (`src/job_agent/adapters/icims.py`): fetches open US postings
  from iCIMS career sites fronted by Jibe's public, unauthenticated
  `/api/jobs` JSON endpoint. A compound `tenant[:host]` slug (e.g.
  `hooli:careers.hooli.com`) in `registry.txt` keeps the `fetch(slug) ->
  list[Posting]` adapter contract unchanged, defaulting `host` to
  `{tenant}.icims.com` when omitted. Pagination walks every page until
  `totalCount` is exhausted, a US-location gate keeps `US` and
  unparseable/missing country codes while dropping postings positively
  identified as non-US, and postings with no description are excluded
  before scoring rather than scored empty. Registered as `"icims"` in
  `fetch.py`'s `ADAPTERS` dispatch table alongside `greenhouse`, `lever`,
  and `workday`. Honors the existing `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER`
  cap, halting pagination once reached, and a per-tenant fetch failure is
  contained so the run continues with the remaining sources. Display-name
  resolution reuses `companies.toml`'s `[display_names]` table (tenant slug
  -> company name), falling back open to the tenant slug itself.

## [1.1.0] - 2026-06-22

### Added

- Workday adapter (`src/job_agent/adapters/workday.py`): fetches open US
  postings from Workday CXS (Career Site eXperience Service) tenants. A
  single compound `tenant:site:host` slug (e.g.
  `globex:Globex:wd5`) in `registry.txt` keeps the
  `fetch(slug) -> list[Posting]` adapter contract unchanged; the module
  splits it internally. Two round-trips per posting: a paginated `POST
  .../wday/cxs/{tenant}/{site}/jobs` (scoped to a US country facet) for the
  list, then a `GET` per posting for the full description. Registered as
  `"workday"` in `fetch.py`'s `ADAPTERS` dispatch table alongside
  `greenhouse` and `lever`.
- `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER`: optional per-tenant cap on postings
  fetched per run, read by the Workday adapter (large boards can report
  100+ open postings for a single tenant).
- `companies.toml.example`: committed template for `companies.toml`
  (git-ignored, personal), which maps Workday tenant slugs to display
  company names for the digest. A tenant with no entry, or a missing file,
  falls back open to the tenant slug itself, since company feeds the
  dedupe fingerprint (`schema.py`) and must never be empty.

## [1.0.0] - 2026-06-22

First stable release. The fetch → score → digest pipeline runs unattended on
Azure Container Apps with CI/CD, missed-deadline + cost alerting, per-run and
retention bounds, and a documented fork-and-deploy path.

### Added

- Retention purge: each run drops postings (and their `applications` rows) older
  than the configured retention window, keeping `jobs.db` bounded over time.
- Cost budget alerting: a `Microsoft.Consumption/budgets` resource notifies the
  configured receivers as monthly Azure spend crosses its thresholds,
  complementing the per-run scoring cost cap.
- `scripts/bootstrap.sh` registers the Azure resource providers
  `infra/main.bicep` depends on (so a fresh subscription's first deploy doesn't
  fail on an unregistered provider) and grants the signed-in maintainer *Key
  Vault Secrets Officer* on the resource group, so the post-deploy
  `az keyvault secret set` commands succeed against the RBAC-authorized vault
  (override the principal with `KV_SECRETS_ADMIN_OBJECT_ID`).
- README "Deploy your own instance" fork guide: what a fork must change
  (subscription/tenant, image, alert receivers, runtime files + secrets,
  optional CI).

### Changed

- Documentation separates forker setup from maintainer acceptance validation:
  `docs/manual-deployment.md` (+ `docs/ci-cd.md`) is the clean step-by-step
  setup path, while `specs/001-azure-deployment/quickstart.md` is the
  maintainer's per-user-story acceptance validation and is no longer presented
  as a fork prerequisite — a new instance no longer has to run the overnight
  alert drill to be usable. Setup steps live in one place, not duplicated across
  both documents.

### Fixed

- Run/force-run docs no longer show the broken
  `az containerapp job start --env-vars JOBAGENT_FORCE=1` form, which replaces
  the whole execution template (dropping the image, secret references, and
  `/data` mount). They now use the merge-safe `job update --set-env-vars` →
  `start` → `update --remove-env-vars` sequence.
- The US3 alert-drill instructions no longer reference a non-existent Key Vault
  secret "rename"; the documented drill parks the schedule for one night.

## [0.2.1] - 2026-06-13

### Fixed

- Continuous deployment was broken on its first run: `az deployment group
  create` failed with Bicep error BCP258 because the required `@secure()`
  receiver parameters (`alertEmail`, `smsCountryCode`, `smsPhone`) were passed
  as inline `--parameters` alongside `infra/main.bicepparam`. A `.bicepparam`
  file is compiled before inline parameters merge, so a required no-default
  parameter must be assigned in the param file itself. `infra/main.bicepparam`
  now reads the three receivers from the environment via
  `readEnvironmentVariable`, and the deploy workflow (and the manual-deploy
  docs) expose them as environment variables instead of inline parameters. The
  values are still never committed, and a missing one fails the compile loudly
  (BCP427) rather than building a receiver-less alert.

### Added

- `scripts/validate-infra.sh`: a local `az bicep build-params` compile-check
  for the infra (no Azure access needed), so a param-file/command-shape
  mismatch like the one above is caught before deploy. The "Green before done"
  quality gate now requires it for changes touching `infra/` or the deploy
  workflow, alongside `uv run pytest`.

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
