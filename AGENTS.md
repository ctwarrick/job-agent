# job-agent — Agent Instructions

This file is the platform-agnostic source of truth for AI agents working in
this repo. `CLAUDE.md` is a symlink to it. Detailed role definitions live in
`agents/*.md`; thin per-platform adapters (e.g. `.claude/agents/`) point at
those cards.

## Project overview

A small pipeline that pulls job postings from company ATS boards, scores them
against a personal profile with Claude, and emails a triaged digest.

Pipeline: **fetch → score → digest**, all stages in `src/job_agent/`:

| Module | Purpose |
|---|---|
| `schema.py` | normalized `Posting` + dedupe fingerprint |
| `store.py` | SQLite: `postings` + `applications` tables |
| `fetch.py` | reads `registry.txt`, dispatches adapters, upserts |
| `score.py` | LLM scoring against `profile.md` + `screening_prompt.md` |
| `digest.py` | emails the daily digest |
| `adapters/` | one module per ATS vendor (`greenhouse.py`, `lever.py`), each exposing `fetch(slug) -> list[Posting]` |

### Commands

```bash
uv sync                    # install deps
uv run pytest              # run the test suite
uv run python main.py      # full pipeline (fetch -> score -> digest)
uv run jobagent-fetch      # single stage
uv run jobagent-score
uv run jobagent-digest
```

### Conventions

- Module docstrings explain the *why* and the data contract, not just the what.
- Stdlib-first; the only heavyweight dependency is `anthropic`.
- Small pure helpers (`_format_postings`, `_score_batch`) over classes.
- Fail loud: stages `sys.exit()` on missing config so `main.py` stops rather
  than emailing a stale/empty digest.
- Config via env vars (`ANTHROPIC_API_KEY`, `JOBAGENT_SALARY_FLOOR`,
  `JOBAGENT_MODEL`); subjective tuning via runtime files, not code.
- Python style: Black, line length 100 (`[tool.black]` in pyproject.toml);
  docstrings and full-line comments wrap at 80; Google-style docstrings with
  Args/Returns/Raises on functions (module docstrings keep the why + data
  contract); type hints on all functions, `Any` only when truly necessary.
  Applied at write time, never as a post-hoc sweep.
- Tests are stub-based with no network calls — follow the patterns in `tests/`.

## Sensitive files — never commit, never quote

`profile.md`, `screening_prompt.md`, `registry.txt`, and `jobs.db` contain
personal data and are git-ignored. Never commit them, and never paste their
contents into plans, PRs, commit messages, or agent summaries.

## Multi-agent workflow

Work flows through phases. Each phase produces a **small artifact** (spec,
plan, review verdict) and the next phase starts from that artifact — never
from the previous phase's raw transcript. That is the context-control
mechanism: transcripts stay inside each role's own context window.

```
Specify → Plan → [HUMAN APPROVES] → Build (TDD: test-writer → implementer)
        → Review → [HUMAN APPROVES] → Commit/PR
        → Release (on request: changelog, version, lock, README;
                   retrospective edits to agents/*.md ship in the same
                   release push, before the tag; [HUMAN APPROVES] tag)
        → Retrospective (recommends role-card refinements)
        → [HUMAN COMMITS RELEASE/RETRO DOCS AND PUSHES TO REMOTE]
```

### Role routing

| Role | Model tier | Handles | Returns |
|---|---|---|---|
| Orchestrator (main session) | top | routing, gates, prompt composition | phase artifacts to the human |
| Scout | haiku | read-only recon: locate code, trace paths, summarize docs | ≤30-line digest with `file:line` refs |
| Planner | top (inherit) | spec + scout digest → implementation plan | plan markdown for the human gate |
| Test-writer | sonnet | failing pytest tests from the approved plan | test paths + red-failure summary |
| Implementer | sonnet | minimal diff to make tests pass | changed files + green pytest summary |
| Reviewer | top (inherit) | fresh-context diff review, runs pytest itself | `APPROVE`/`REVISE` + numbered findings |
| Releaser | sonnet | release prep: CHANGELOG, version bump + `uv lock`, README drift check | proposed version + changelog section + README edits |
| Retrospective | top (inherit) | post-ship analysis of agent performance | recommended edits to `agents/*.md` |

Model-tier rationale: spend top-model tokens where judgment is the product
(orchestration, architecture, the final bug-catching gate, meta-review); use
sonnet where the task is well-scoped by an approved plan; use haiku where the
work is mechanical search.

### Delegation rules (token discipline)

1. Don't spawn an agent for what a single read or grep answers — do it inline.
2. Every dispatch gives the subagent explicit file paths and one narrow
   question or task, plus the expected output format.
3. Subagents return structured summaries (findings + `file:line` refs), never
   file dumps or full transcripts.
4. One role per dispatch. A role that finds itself doing another role's job
   stops and hands back instead.
5. Phase artifacts are the only thing that crosses phase boundaries.

### Context budget protocol

Model performance degrades well before the context window is full, and
autocompact (which *summarizes* — lossy) only fires near the hard limit. We
get ahead of both. A hook (`.claude/hooks/context_monitor.py`, registered in
`.claude/settings.json`) measures real token usage after every tool call and
user prompt against an **effective budget** =
`min(200K performance budget, 80% of the current model's window)`. It is
model-aware: a Haiku subagent (200K window) is measured against 160K, while
1M-window models are measured against the flat 200K sweet-spot budget.
Override the budget with env `CONTEXT_BUDGET`.

| Band | Trigger | Required action |
|---|---|---|
| ELEVATED | 60% | finish the current phase, then write/refresh all phase artifacts under `docs/work/<task>/` |
| HIGH | 75% | write `docs/work/<task>/handoff.md` (state, decisions, next steps, open questions); recommend a fresh session; start no new phases |
| CRITICAL | 85% | stop dispatching; write/update the handoff immediately |

Principles:

1. **Nothing is dropped.** Artifacts and the handoff live on disk, so a fresh
   session resumes with zero loss. Never summarize-in-place as a substitute
   for checkpointing.
2. **Checkpoint continuously, not reactively.** Phase artifacts are written
   to `docs/work/<task>/` at every phase boundary from the start — a band
   warning should normally be a confirmation, not a scramble.
3. **Resume from disk.** A fresh session starts by reading
   `docs/work/<task>/handoff.md` plus the phase artifacts — never by
   replaying old conversation history.

### Quality gates (hard rules)

1. **TDD ordering**: failing tests are authored and shown red *before* any
   implementation code is written.
2. **Green before done**: no task is "done" until `uv run pytest` passes; the
   reviewer re-runs it rather than trusting the implementer's claim.
3. **Independent review**: the reviewer gets a fresh context with only the
   diff and the plan — never the implementer's transcript.
4. **Human gates**: the plan needs explicit human approval before build, and
   nothing is committed, pushed, tagged, or published as a release without
   explicit human go-ahead.

## Role card index

- `agents/orchestrator.md` — main-session behavior: routing, gates, dispatch prompts
- `agents/scout.md` — read-only recon (haiku)
- `agents/planner.md` — implementation plans (top)
- `agents/test-writer.md` — red phase: failing tests (sonnet)
- `agents/implementer.md` — green phase: minimal diff (sonnet)
- `agents/reviewer.md` — independent diff review (top)
- `agents/releaser.md` — release prep: changelog, version, lock, README (sonnet)
- `agents/retrospective.md` — post-ship agent-performance review (top)

<!-- SPECKIT START -->
For additional context about technologies to be used, project structure,
shell commands, and other important information, read the current plan
at specs/001-azure-deployment/plan.md
<!-- SPECKIT END -->
