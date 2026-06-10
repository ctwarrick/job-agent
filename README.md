# Job Agent

A small pipeline that pulls postings straight from company ATS boards, scores
them against your profile with Claude (substance over title), and is ready to
email you a triaged digest.

## Layout
```
job-agent/
  pyproject.toml
  main.py              pipeline entry point (fetch -> score -> digest)
  registry.txt         your target companies -> ATS vendor + slug (git-ignored, personal)
  profile.md           YOUR profile (git-ignored, personal)
  screening_prompt.md  LLM screening prompt (git-ignored, personal)
  src/job_agent/
    schema.py          normalized Posting + dedupe fingerprint
    store.py           SQLite: postings + applications tables
    fetch.py           reads registry, dispatches adapters, upserts
    score.py           LLM scoring against profile.md + screening_prompt.md
    digest.py          emails the daily digest
    adapters/
      __init__.py
      greenhouse.py    public Greenhouse board API
      lever.py         public Lever postings API
  tests/               pytest suite
```

## Setup
```bash
uv sync
export ANTHROPIC_API_KEY=sk-...
```

## Run
The whole pipeline in one process (the container / scheduled-job entry point):
```bash
uv run python main.py
```
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
   Dedupe is by title+company+location hash, so cross-posts collapse.
   `INSERT OR IGNORE` keeps re-runs idempotent: scores and dismissals survive.
2. **score** — batches unscored postings, scores each on skills_fit,
   seniority_fit, category_risk (career pull), plus bucket / comp_flag /
   trajectory_note. Judges SUBSTANCE, ignores title and Scrum vocabulary.
3. **digest** (to build) — single SELECT joining postings + applications where
   status='new' and thresholds met, sorted by fit; email it.

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

## Suggested digest query (for digest.py)
```sql
SELECT p.title, p.company, p.url, p.skills_fit, p.category_risk, p.rationale
FROM postings p JOIN applications a USING(fingerprint)
WHERE a.status='new' AND p.skills_fit >= 6 AND p.category_risk <= 4
ORDER BY p.skills_fit DESC, p.category_risk ASC;
```
