# Job Agent

A small pipeline that pulls postings straight from company ATS boards, scores
them against your profile with Claude (substance over title), and is ready to
email you a triaged digest.

## Layout
```
job-agent/
  pyproject.toml
  main.py              pipeline entry point + run-lifecycle wrapper (fetch -> score -> digest)
  conftest.py          repo-root pytest config (lets tests `import main`)
  Dockerfile           runtime container image (uv sync --frozen)
  .dockerignore        excludes personal/runtime files from the image
  registry.toml        your target companies -> ATS sources (git-ignored, personal)
  registry.toml.example  committed template for registry.toml
  profile.md           YOUR profile (git-ignored, personal)
  screening_prompt.md  LLM screening prompt (git-ignored, personal)
  filter.toml          deterministic pre-LLM filter (git-ignored, personal)
  filter.toml.example  committed template for filter.toml
  src/job_agent/
    schema.py          normalized Posting + dedupe fingerprint
    store.py           SQLite: postings + applications + runs tables
    fetch.py           reads registry, dispatches adapters, upserts
    filter.py          deterministic pre-LLM gate (denylist/allowlist/age/location)
    score.py           LLM scoring against profile.md + screening_prompt.md
    digest.py          emails the daily digest
    adapters/
      __init__.py
      greenhouse.py    public Greenhouse board API
      lever.py         public Lever postings API
      workday.py       Workday CXS (Career Site eXperience Service) API
      icims.py         iCIMS career sites via the public Jibe /api/jobs API
      talemetry.py     Talemetry / TTC-Portals careers site (HTML scrape)
  tests/               pytest suite
  infra/               Azure Bicep (Container Apps Job, Key Vault, storage)
  scripts/             deployment bootstrap scripts
  docs/                manual-deployment.md and other docs
```

## Setup
```bash
uv sync
export ANTHROPIC_API_KEY=sk-...
export JOBAGENT_SALARY_FLOOR=120000   # base salary floor in dollars; required by score
export SMTP_HOST=smtp.example.com     # required by digest
export SMTP_PORT=587
export SMTP_USER=you@example.com
export SMTP_PASS=...
export DIGEST_TO=you@example.com      # optional, defaults to SMTP_USER
```

Optional env vars:
- `JOBAGENT_DATA_DIR` — directory holding `jobs.db`, `registry.toml`,
  `profile.md`, and `screening_prompt.md` (default: current directory).
  Lets all stages run against a relocated/mounted data dir (e.g. an Azure
  Files share) with no path changes.
- `JOBAGENT_TZ` — timezone for the daily `digest_date` (default
  `America/Los_Angeles`).
- `JOBAGENT_MODEL` — Anthropic model used for scoring (default
  `claude-sonnet-4-6`).
- `JOBAGENT_MAX_POSTINGS_PER_RUN` / `JOBAGENT_MAX_COST_PER_RUN` — per-run
  guardrails for the score stage (defaults `200` postings and `5.00` USD
  estimated). The run stops cleanly at whichever cap is hit first, logs
  `SCORE_CAP_STOP`, exits 0, and the next run resumes the remainder.
- `JOBAGENT_PRICE_INPUT` / `JOBAGENT_PRICE_OUTPUT` /
  `JOBAGENT_PRICE_CACHE_WRITE` / `JOBAGENT_PRICE_CACHE_READ` — per-MTok prices
  used for the cost cap and the `SCORE_SUMMARY` estimate (defaults `3`, `15`,
  `3.75`, `0.30` — the `claude-sonnet-4-6` rates). Override when the model or
  pricing changes.
- `JOBAGENT_MAX_POSTINGS_PER_EMPLOYER` — optional cap on postings fetched per
  tenant by the Workday, iCIMS, and Talemetry adapters (some boards report
  100+ open postings for a single tenant).
- `JOBAGENT_FORCE` — set to `1` to re-run a digest_date that already
  succeeded today.
- `DIGEST_MIN_SKILLS` / `DIGEST_MAX_RISK` — digest thresholds (defaults `6`
  and `4`).
- `DIGEST_DRY_RUN` — set to `1` to print the digest instead of sending it.

## Run
The whole pipeline in one process (the container / scheduled-job entry point):
```bash
uv run python main.py
```
`main.py` wraps fetch -> score -> digest with a run-lifecycle check: it skips
a digest_date that's already in flight or already succeeded today (unless
`JOBAGENT_FORCE=1`), and prints `RUN_SUCCESS` / `RUN_FAILED_FINAL` markers for
monitoring.

Or run a single stage on its own:
```bash
uv run jobagent-fetch     # pull + store new postings
uv run jobagent-score     # score the unscored ones
uv run jobagent-digest    # email high-fit, low-risk, not-yet-sent postings
```

## Tests
```bash
uv run pytest
```

## Pipeline
1. **fetch** — one adapter per ATS vendor returns a common `Posting` schema.
   Dedupe is a hash of title+company+location+description, so cross-posts
   collapse while distinct postings with the same title/company/location
   stay separate. `INSERT OR IGNORE` keeps re-runs idempotent: scores and
   dismissals survive.
2. **score** — batches unscored postings, scores each on skills_fit,
   seniority_fit, category_risk (career pull), plus bucket / comp_flag /
   trajectory_note. Judges SUBSTANCE, ignores title and Scrum vocabulary.
3. **digest** — single SELECT joining postings + applications where
   status='new', not yet sent, and thresholds met, sorted by fit; emails the
   result over SMTP (or a "no new matches" notice on an empty day) and marks
   sent postings so they aren't repeated.

## Cloud deployment
For running the pipeline as a scheduled Azure Container Apps Job, see
[`docs/manual-deployment.md`](docs/manual-deployment.md) for the initial
bootstrap/deploy, then [`docs/ci-cd.md`](docs/ci-cd.md) to activate
push-to-`main` continuous deployment (test -> build -> deploy, no manual
step once enabled). Deployment also provisions a missed-deadline alert
(email + SMS) if the daily digest hasn't run by the configured local
deadline.

## Deploy your own instance
This is the maintainer's repo, but it is forkable. The step-by-step setup is
[`docs/manual-deployment.md`](docs/manual-deployment.md) (manual deploy), then
[`docs/ci-cd.md`](docs/ci-cd.md) to activate push-to-`main` auto-deploys;
[`specs/001-azure-deployment/quickstart.md`](specs/001-azure-deployment/quickstart.md)
is the maintainer's acceptance validation, not part of fork setup. What a fork
must change:

- **Your own subscription + tenant.** Pass them to `scripts/bootstrap.sh` with
  your own `GITHUB_REPO` slug (`<you>/job-agent`) so the deploy identity's
  federated credential trusts *your* fork. Bootstrap also registers the Azure
  resource providers the template needs and grants you Key Vault Secrets Officer
  so the secret-set step below works against the RBAC-authorized vault.
- **Your own image.** Build and push to `ghcr.io/<you>/job-agent`, then make the
  package public (or grant the Container Apps environment pull access).
- **Your own alert receivers.** `ALERT_EMAIL`, `SMS_COUNTRY_CODE`, and
  `SMS_PHONE` are required (no defaults) and are personal data — export them at
  deploy time (or as your fork's repo secrets for CI), never in
  `infra/main.bicepparam`. They wire the missed-deadline and cost-budget alerts.
- **Your own runtime files + secrets.** Upload your git-ignored `profile.md`,
  `screening_prompt.md`, `registry.toml`, and `filter.toml` to the Files share,
  and set the seven Key Vault secrets (`anthropic-api-key`, `smtp-host`/`-port`/
  `-user`/`-pass`, `digest-to`, `salary-floor`). None of these are ever committed.
- **CI (optional).** To activate push-to-`main` deploys, record your fork's OIDC
  `client-id` / `tenant-id` / `subscription-id` as repo secrets per
  [`docs/ci-cd.md`](docs/ci-cd.md).

Alerting deploys with the infrastructure, so a silently failed night pages you
(email + SMS) by ~06:30. Verify your own receivers in seconds with the
action-group `test-notifications` command in
[`docs/manual-deployment.md`](docs/manual-deployment.md) — you don't need to run
the maintainer's overnight alert drill to use the bot.

## Adding a company
Add a `[[source]]` table to `registry.toml` (copy `registry.toml.example`).
Find their careers page and read the redirect URL for the fields:
  boards.greenhouse.io/SLUG                -> vendor `greenhouse`, slug `SLUG`
  jobs.lever.co/SLUG                       -> vendor `lever`, slug `SLUG`
  {tenant}.{host}.myworkdayjobs.com/{site} -> vendor `workday`, tenant/site/host
  {tenant}.icims.com (or a custom domain)  -> vendor `icims`, tenant (+ optional host)
  a Talemetry / TTC-Portals careers host    -> vendor `talemetry`, host
```toml
[[source]]
vendor = "workday"
name   = "Globex Corporation"   # optional: the digest/dedupe company name
tenant = "globex"
site   = "Globex"
host   = "wd5"
```
The optional `name` is the authoritative company for the digest (and the
dedupe fingerprint); omit it and greenhouse/lever fall back to the slug,
workday/icims to the tenant. Set `enabled = false` to park a source without
deleting it. The loader fails loud on a typo or missing required field. Done.

## Adding a new ATS (Ashby, Workable, SmartRecruiters...)
Write `src/job_agent/adapters/<vendor>.py` exposing
`fetch(slug, *, company=None, timeout=20) -> list[Posting]`, then register it
in `ADAPTERS` in fetch.py. The scorer needs no changes.

## Tuning the scorer
Everything subjective lives in three runtime files the scorer reads — no code
changes needed:
- `profile.md` — the candidate profile (who you are, what you want).
- `screening_prompt.md` — the LLM system prompt / screening instructions.
- `filter.toml` — the deterministic pre-LLM gate (denylist / advisory
  allowlist / age / location) that drops obviously-irrelevant postings before
  they cost a Claude call. Copy the committed `filter.toml.example` to
  `filter.toml` and tune it; the score stage fails loud if it is missing or
  malformed.

All three contain personal data and are git-ignored (only `filter.toml.example`
is committed). To re-score from scratch after editing any of them:
  sqlite3 jobs.db "UPDATE postings SET skills_fit=NULL, filter_reason=NULL"
then `uv run jobagent-score` again.
