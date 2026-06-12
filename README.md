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
  registry.txt         your target companies -> ATS vendor + slug (git-ignored, personal)
  profile.md           YOUR profile (git-ignored, personal)
  screening_prompt.md  LLM screening prompt (git-ignored, personal)
  src/job_agent/
    schema.py          normalized Posting + dedupe fingerprint
    store.py           SQLite: postings + applications + runs tables
    fetch.py           reads registry, dispatches adapters, upserts
    score.py           LLM scoring against profile.md + screening_prompt.md
    digest.py          emails the daily digest
    adapters/
      __init__.py
      greenhouse.py    public Greenhouse board API
      lever.py         public Lever postings API
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
- `JOBAGENT_DATA_DIR` — directory holding `jobs.db`, `registry.txt`,
  `profile.md`, and `screening_prompt.md` (default: current directory).
  Lets all stages run against a relocated/mounted data dir (e.g. an Azure
  Files share) with no path changes.
- `JOBAGENT_TZ` — timezone for the daily `digest_date` (default
  `America/Los_Angeles`).
- `JOBAGENT_MODEL` — Anthropic model used for scoring (default
  `claude-sonnet-4-6`).
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
[`docs/manual-deployment.md`](docs/manual-deployment.md).

## Adding a company
Find their careers page, read the redirect URL:
  boards.greenhouse.io/SLUG  -> `greenhouse  SLUG`
  jobs.lever.co/SLUG         -> `lever  SLUG`
Add a line to registry.txt. Done.

## Adding a new ATS (Ashby, Workable, SmartRecruiters, Workday...)
Write `src/job_agent/adapters/<vendor>.py` exposing
`fetch(slug) -> list[Posting]`, then register it in `ADAPTERS` in fetch.py.
The scorer needs no changes.

## Tuning the scorer
Everything subjective lives in two runtime files the scorer reads — no code
changes needed:
- `profile.md` — the candidate profile (who you are, what you want).
- `screening_prompt.md` — the LLM system prompt / screening instructions.

Both contain personal data and are git-ignored. To re-score from scratch after
editing either:
  sqlite3 jobs.db "UPDATE postings SET skills_fit=NULL"
then `uv run jobagent-score` again.
