# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once it reaches 1.0.0. Before 1.0.0, minor versions may include breaking changes.

## [3.0.0] - 2026-07-10

Permanent fix for the overnight-run scaling problem that 2.4.2 band-aided:
the fetch stage now runs boards concurrently against a stage-wide budget
inside a 2-hour execution window, instead of relying on a longer sequential
timeout alone. Spec'd in `specs/007-overnight-scale/spec.md`.

### Added

- Stage-wide fetch budget (FR-003): `fetch.main()` computes a wall-clock
  `stage_deadline` once up front from the new `JOBAGENT_FETCH_BUDGET_SECONDS`
  (default `5400`, 90 min) and stops submitting new boards once it passes,
  rather than letting the fetch stage run unbounded and crowd out scoring and
  the digest. A board never dispatched is reported as a new degraded-digest
  category, "budget-deferred" (`reason="budget_deferred"`), distinct from a
  wholly-failed or partially-fetched source, and is never marked converged so
  it sorts first on the next run (no starvation).
- Startup coherence check (FR-004, `main.py`): before `store.init()` or any
  other external effect, the run validates that
  `JOBAGENT_FETCH_BUDGET_SECONDS` plus the new
  `JOBAGENT_SCORE_DIGEST_HEADROOM_SECONDS` (default `1800`, 30 min reserved
  for scoring + the digest) fits inside the new required
  `JOBAGENT_EXECUTION_WINDOW_SECONDS`. A misconfigured budget/headroom/window
  combination fails loud with a diagnosable message instead of letting the
  platform kill an in-flight run with no trace.
- Least-recently-fetched dispatch ordering (FR-006): a new
  `store.sources_by_recency()` ranks every registered `(vendor, company)` by
  its last-converged timestamp (never-fetched first), and `fetch.main()`
  dispatches boards in that order across the whole registry, so the oldest or
  never-fetched board is served first regardless of registry position.
  Greenhouse/Lever (single-request, whole-board-in-one-call vendors) now call
  the new `store.mark_converged()` on a clean fetch too, extending the
  resilient adapters' existing convergence bookkeeping to give the ordering a
  total, registry-wide signal.
- Board-level concurrent fetch (FR-007/FR-008): `fetch.main()` dispatches up
  to `JOBAGENT_FETCH_CONCURRENCY` (default `8`) boards in parallel through a
  bounded `ThreadPoolExecutor`. A new module-level `_STORE_LOCK` serializes
  every SQLite write (and each board's summary log line) across worker
  threads, since the store's default rollback-journal connection has no busy
  timeout; the adapter/network call itself runs off-lock, which is where the
  concurrency comes from. A new `_LockingStore` proxy is handed to
  `resilient.run_source` so a two-phase (Workday/iCIMS/Talemetry) board's
  slow listing/description fetches parallelize with other boards while only
  its individual store ops serialize. One board raising is contained to its
  own failed-source record and never blocks its siblings.
  `JOBAGENT_FETCH_CONCURRENCY=1` reproduces the prior strict
  submit-one/wait-one sequential path and ordering exactly.
- `infra/main.bicep` / `infra/main.bicepparam`: `replicaTimeoutSeconds` is now
  `7200` (2 hours, up from the 2.4.2 stopgap's `2700`), `cronExpression` is
  `'0 8,10,12 * * *'` (three attempts spaced to match the new window), and the
  Container Apps Job passes `JOBAGENT_EXECUTION_WINDOW_SECONDS` set to
  `string(replicaTimeoutSeconds)` — the app validates against the exact
  deadline the platform enforces, single source of truth.

### Changed

- `digest.py` / `main.py`: the degraded-run summary now names a
  budget-deferred count separately from failed and partial source counts
  (e.g. "2 sources failed; 1 source partial; 1 board deferred; 919 unscored
  (cap=cost)"), so a budget-bound night is distinguishable from an adapter
  failure or a per-source backstop truncation without reading logs.

### Breaking

- `JOBAGENT_EXECUTION_WINDOW_SECONDS` is a new environment variable with no
  code default, and `main.py` now fails loud (`sys.exit`) at startup if it is
  unset. Production Bicep deploys already set it (see above), so a deployed
  instance is unaffected once redeployed on this version, but a local/dev
  invocation of `uv run python main.py` that previously ran with no extra
  config must now export it explicitly (e.g. `export
  JOBAGENT_EXECUTION_WINDOW_SECONDS=7200`). This mirrors the precedent set by
  2.0.0's required `registry.toml`: an unset required input fails loud rather
  than silently running against a window the platform doesn't actually honor.

## [2.4.2] - 2026-07-09

### Fixed

- **Overnight job missed its window**: all three scheduled attempts of
  `jobagent-job` on 2026-07-09 failed with `DeadlineExceeded` — the registry
  has grown to ~35 Workday-heavy boards, and a full sequential fetch now
  takes ~20-25 min, exceeding the old 15-min `replicaTimeout` (900s,
  `main.bicep`'s default). No digest was delivered and the missed-deadline
  alert fired. `infra/main.bicepparam` now sets `param replicaTimeoutSeconds
  = 2700` (45 min, ~2x the observed fetch time) and moves `cronExpression`
  from `'0,20,40 11 * * *'` to `'0 10,11,12 * * *'` (three attempts hourly
  at 10:00/11:00/12:00 UTC) so retries no longer overlap into a
  startup-check no-op, all three still fire after local midnight in both
  PST and PDT, and the last attempt finishes before the 06:00 delivery
  deadline. The live Azure job was already updated via `az containerapp job
  update` to match on the night of the incident; this release records the
  IaC change. This is a stopgap for the enlarged registry; the permanent
  fix (a longer window plus parallel fetch and a fetch-stage budget) is
  spec'd separately in `specs/007-overnight-scale/spec.md`.

## [2.4.1] - 2026-07-08

### Fixed

- **Budget redeploy failure**: the v2.4.0 deploy failed with Azure error
  "Start date of budgets cannot be updated" — a
  `Microsoft.Consumption/budgets` `startDate` is immutable after creation,
  but `infra/main.bicep` defaulted `budgetStartDate` to the first of the
  *current* UTC month via `utcNow`, so any redeploy in a later month than the
  live budget's creation month computed a different date and 400'd.
  `infra/main.bicepparam` now pins `param budgetStartDate =
  '2026-06-01T00:00:00Z'` to the existing `jobagent-monthly` budget's actual
  creation date; the `utcNow` default in `infra/main.bicep` remains correct
  only for a fresh environment's first deploy. `@description` and the
  comment block above the `budget` resource in `infra/main.bicep` are
  corrected to state the immutability rule instead of claiming the default
  makes every redeploy safe.

### Changed

- `scripts/review-codex.sh`: the sandboxed Codex reviewer can now re-run
  `scripts/validate-infra.sh` on infra diffs. It seeds
  `AZURE_CONFIG_DIR=/tmp/jobagent-azure` with `bin/bicep` symlinked to the
  az-managed binary at `~/.azure/bin/bicep` (a bare override loses the
  binary, and the sandbox has no network to re-fetch it), and exports
  `DOTNET_BUNDLE_EXTRACT_BASE_DIR=/tmp/jobagent-dotnet` since bicep's .NET
  single-file bundle self-extracts under `$HOME`, which is read-only in the
  sandbox.

## [2.4.0] - 2026-07-08

### Added

- `scripts/review-codex.sh`: dispatches the Reviewer role cross-vendor to
  GPT-5.5 via the OpenAI Codex CLI (`codex exec --sandbox workspace-write`),
  so the final bug-catching gate no longer shares a model family with the
  Claude roles that planned and implemented the change. Usage:
  `scripts/review-codex.sh <diff-range> <plan-path> [verdict-out]`; the
  reviewer runs `uv run pytest` itself inside the sandbox (the script exports
  `UV_CACHE_DIR`, default `/tmp/jobagent-uv-cache`, since uv's default
  `~/.cache/uv` is read-only there) and emits the same `APPROVE`/`REVISE` +
  numbered-findings contract as the Claude reviewer subagent. The model is
  overridable via `JOBAGENT_REVIEW_MODEL` for ChatGPT tiers that reject
  `gpt-5.5`. Prerequisite: Codex CLI installed and authenticated via `codex
  login` (ChatGPT plan — subscription quota, not API billing).

### Changed

- Reviewer role card (`agents/reviewer.md`) and orchestrator (`agents/
  orchestrator.md` step 4): the primary review runner is now GPT-5.5 via
  `scripts/review-codex.sh`; if the script exits non-zero (missing/expired
  Codex auth, rejected model, network down), the orchestrator automatically
  falls back to the Claude `reviewer` subagent and flags the fallback to the
  human as a same-vendor review. Gate records now name the runner used
  ("codex exec / gpt-5.5" or "claude reviewer subagent (FALLBACK,
  same-vendor)").
- `AGENTS.md` role routing table, model-tier rationale, and commands list
  updated to match: the Reviewer row names `scripts/review-codex.sh` and its
  Codex CLI / `codex login` / `JOBAGENT_REVIEW_MODEL` prerequisites.
- `.claude/agents/reviewer.md` frontmatter now describes this Claude subagent
  as the fallback runner, used only when `scripts/review-codex.sh` fails;
  its behavior is otherwise unchanged.
- `.claude/settings.json` is now local and untracked (it also holds
  per-user permission greenlists); a tracked `.claude/settings.json.example`
  ships the hooks-only config for forkers to copy, and `.gitignore` gains a
  `.claude/settings.json` entry.

## [2.3.0] - 2026-06-30

### Fixed

- **Workday facet fallback**: tenants that do not expose the US country facet
  now return HTTP 400 on any request that applies it. The adapter detects this
  via the new `_is_bad_request` helper, drops the facet, and retries the page
  facet-free, so those sources produce results instead of failing the source
  outright. The downstream location filter still scopes to the US, so the
  only cost is additional list pages, not extra detail fetches.
- **Workday pagination**: Workday echoes `total: 0` on every page after the
  first while still serving full result pages, causing the walk to stop after
  one page on multi-page boards. The adapter now captures `total` once from
  the first page and uses that count for the remainder of the walk, stopping
  on the captured total or on a short/empty page rather than on a later page's
  bogus zero.

### Changed

- **Alert action-group short name**: the Azure action-group `groupShortName`
  (the prefix that appears on alert SMS messages and email subjects) is now
  driven by a dedicated `alertShortName` param (default `JobAgent`,
  `@maxLength(12)`) instead of `take(sanitizedPrefix, 12)`, which rendered as
  the mangled seven-character string `jobagen`. The param has a sensible default
  and does not require a `main.bicepparam` entry unless the forker wants to
  override it.
- **Missed-deadline alert display name and description**: the rule's
  `displayName` is rewritten to `{alertShortName}: morning digest missed its
  deadline` so it reads correctly under both Azure's `Fired:` and `Resolved:`
  prefixes. The `description` is expanded to name the recommended action and to
  explain that a `Resolved` notification means the pipeline recovered on its
  own and no action is needed (the rule has `autoMitigate: true`).

## [2.2.0] - 2026-06-27

Fixes the production outage where a large Workday board exhausted the daily
run's execution window before scoring or digest ever ran.

### Added

- `src/job_agent/resilient.py`: a shared two-phase fetch contract
  (`run_source`) for adapters whose boards require per-posting detail
  retrieval or unbounded pagination. Listing fields (title, location,
  posting date) are fetched first and run through the deterministic filter;
  the expensive per-posting description is fetched only for postings that
  survive, so request volume scales with filter survivors, not board size.
  A per-source backstop — a cap on detail retrievals
  (`JOBAGENT_MAX_DETAIL_PER_SOURCE`, default 150) and a wall-clock deadline
  (`JOBAGENT_FETCH_DEADLINE_SECONDS`, default 300) — stops a pathological
  board loudly rather than letting it exhaust the run. A per-item failure
  (one bad detail page or listing page) is logged and skipped without
  discarding postings already collected. A backstop-truncated source makes
  forward progress on the untouched remainder on the next run rather than
  re-truncating the same prefix, converging to full coverage within
  `JOBAGENT_STALENESS_BOUND_DAYS` (default 7); a source that never converges
  is surfaced as a persistent degradation rather than drifting silently.
- New `source_progress` table (`store.py`) and helper functions tracking
  each resilient source's cursor and truncation history across runs, so
  forward progress survives process restarts.
- `digest.py` gains a partial/degraded source category, distinct from both a
  healthy source and a wholly-unreachable one: a source that was only
  partly fetched (per-item skips, a backstop cutoff, or persistent
  staleness) is named in the digest with the skipped count and whether the
  backstop fired, so the loss is visible without reading logs. `main.py`
  folds these partial sources into the run's `degraded` outcome alongside
  wholly-failed sources and an unscored backlog.

### Changed

- Workday, iCIMS, and Talemetry adapters (the inactive Talemetry adapter
  included, so the fix lands before it ever goes live) now expose a
  two-phase `list_postings` + `fetch_description` contract and are driven
  through `resilient.run_source` instead of returning a single `fetch()`
  list. Per-page fetching tolerates an HTTP error, a timeout, or a
  non-JSON/unparseable body on one page without losing postings already
  collected from earlier pages. Greenhouse and Lever (single-request,
  inline-description boards) are unchanged.
- `fetch.main()` now returns a `(failed_sources, partial_sources)` tuple
  instead of a single list, so callers can distinguish wholly-unreachable
  sources from partially-fetched ones; `main.py` is the only caller and is
  updated accordingly.
- `store.upsert_postings()` now returns only the count of newly inserted
  postings, no longer inflated 2x by the companion `applications`-row
  insert; fetch log lines report the true "new" count.
- The registry `max_per_employer` source key is now accepted but ignored
  (documented as deprecated in `registry.toml.example`) rather than
  enforced — per-source volume is now governed by the backstop above, which
  applies regardless of board size instead of dropping a board's long tail.

### Removed

- `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER`: superseded by the per-source
  backstop (`JOBAGENT_MAX_DETAIL_PER_SOURCE` /
  `JOBAGENT_FETCH_DEADLINE_SECONDS`) above. A board that previously relied
  on this cap to stay small is now fetched in full, subject only to the
  backstop and the deterministic filter.

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
