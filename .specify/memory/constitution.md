<!--
Sync Impact Report
- Version change: 1.0.0 → 1.1.0
- Bump rationale: MINOR — new principle added (VII. LLM Spend Efficiency),
  concretizing Principle I's bounded-LLM-spend requirement; no principle removed
  or redefined.
- Modified principles:
  - I. Cost Discipline — LLM-spend bullet now cross-references Principle VII for the
    concrete mechanics (deterministic pre-filtering, prompt caching, per-run cap,
    cost logging)
- Added sections:
  - VII. LLM Spend Efficiency (NON-NEGOTIABLE) — filter before the LLM, cache the
    static prefix, configurable per-run budget cap, per-run cost observability
- Removed sections: none
- Motivation: the initial cold-start scoring run cost $6.12 for 366 of 1,285
  postings (~$21.5 full backlog), recurring per newly added company in proportion
  to its board size; this surfaced that Principle I's "upper bound on LLM calls"
  was ungoverned in its mechanics and unenforced in code.
- Templates:
  - ✅ .specify/templates/plan-template.md — Constitution Check is generic
    ("[Gates determined based on constitution file]"); picks up VII automatically
  - ✅ .specify/templates/spec-template.md — no constitution-dependent content
  - ✅ .specify/templates/tasks-template.md — no constitution-dependent content
  - ✅ AGENTS.md / CLAUDE.md — consistent (VII aligns with config-via-env,
    fail-loud, and runtime-file tuning); no edits required
- Follow-up TODOs:
  - Concrete pre-filter criteria (buckets / title keywords / location / seniority /
    posting age / salary floor) are deferred to a forthcoming /speckit-specify +
    /speckit-clarify, not encoded here.
  - Azure tenant ID remains deployment configuration; never committed.
-->

# job-agent Constitution

## Core Principles

### I. Cost Discipline (NON-NEGOTIABLE)

This project is personally funded. Every architectural, infrastructure, and dependency
choice MUST minimize recurring cost:

- Total spend across all providers (Azure, Anthropic API, and anything added later)
  MUST stay under **$50/month all-in**. This ceiling is a starting estimate, not a law
  of nature: it may be raised or lowered, but only by explicit maintainer approval
  recorded as a constitution amendment.
- Compute MUST be consumption-based and scale to zero. A workload that runs once per
  day MUST NOT reserve always-on capacity.
- LLM spend MUST be bounded: only unscored postings are sent for scoring, batching is
  preferred, the model is selectable via configuration (`JOBAGENT_MODEL`), and each
  run MUST enforce an upper bound on LLM calls so a defect cannot silently burn
  budget. The concrete mechanics of bounded LLM spend — deterministic pre-filtering,
  prompt caching, per-run caps, and cost logging — are governed by Principle VII.
- Spend MUST be monitored where it is billed. An Azure budget alert alone is not
  sufficient — LLM usage is billed by Anthropic, outside Azure's visibility — so both
  bills are watched.
- Every implementation plan MUST state the expected monthly cost impact of new cloud
  resources, and a cheaper alternative MUST be considered before any paid tier is
  adopted.

Rationale: the product's value (a morning digest) is small and fixed; the spend must
stay proportionate to it.

### II. Cloud-Native Scheduled Operation

The production system runs in Azure, unattended, on a daily overnight schedule.

- Production execution MUST NOT depend on any local machine, local cron, or a human
  pressing a button. Success is defined as: the triaged digest email is in the
  maintainer's inbox by the scheduled delivery time (default 06:00
  America/Los_Angeles — see Operational Constraints).
- All Azure infrastructure MUST be defined as code in this repository. The Azure
  portal is never the system of record; production MUST be reproducible from the repo
  plus secrets. This also serves a deliberate secondary goal: building the
  maintainer's infrastructure-as-code practice.
- The pipeline (`main.py`: fetch → score → digest) is the single scheduled entry
  point and MUST remain runnable locally with only environment variables and the
  git-ignored runtime files, so development and production share one code path.
- Exception — personal runtime files: `profile.md`, `screening_prompt.md`, and
  `registry.toml` cannot travel through git (Principle VI), so they are updated in
  Azure manually through a private mechanism chosen at plan time. This is the single
  sanctioned manual production operation.
- All other configuration is supplied via environment (or the platform's secret
  store); nothing environment-specific is hardcoded.

Rationale: the application's reason to exist is autonomous overnight delivery;
anything beyond the sanctioned exception that requires manual operation is a
regression of the core requirement.

### III. Test-First Delivery (NON-NEGOTIABLE)

- TDD is mandatory: failing tests are written and observed red before implementation
  code; `uv run pytest` MUST pass before any work is called done.
- Tests are stub-based with no network calls, following the existing patterns in
  `tests/`.
- Implementation work is performed by the agent fleet under the workflow in
  `AGENTS.md`; the maintainer alone decides when work is complete and pushes to
  `main`.
- Every push to `main` triggers GitHub Actions: the full test suite runs first, and
  deployment to Azure happens only on green. A red suite blocks deployment — no
  manual override.

Rationale: the human gate stands between unfinished work and `main`; the automated
gate stands between a bad commit and a broken overnight run.

### IV. Simplicity & Stdlib-First

- Prefer the Python standard library; every third-party dependency MUST be justified
  (currently `anthropic` and `requests`).
- Prefer small pure functions over classes; one module per pipeline stage; one
  adapter module per ATS vendor exposing `fetch(slug) -> list[Posting]`.
- Subjective behavior (profile, screening criteria) lives in runtime files, not code.
- Infrastructure follows the same rule: the fewest Azure resources that satisfy
  Principles I and II. New components require justification against a simpler
  alternative in the plan's Complexity Tracking table.

Rationale: a one-person project survives on low maintenance surface; every component
added is a component the sole maintainer must operate at 2 a.m.

### V. Fail Loud, Fail Visibly

- Configuration errors (missing or invalid config) MUST fail hard before any external
  effect: exit non-zero, send nothing.
- Transient per-source failures MUST NOT kill the run: if one ATS board is down or
  misbehaving, the pipeline degrades gracefully, delivers the digest from the sources
  that succeeded, and reports the degradation visibly in the digest itself.
- Failure notification MUST NOT depend solely on the digest email path; a run failure
  surfaces through a channel that still works when email sending is broken (job
  status, platform alert — mechanism specified at plan time).
- The application MUST emit logs sufficient to diagnose a failed or degraded run the
  next morning without re-running it interactively — observability proportionate to a
  single daily batch job, not an enterprise stack.

Rationale: the system runs while its only operator sleeps. A missing email triggers a
morning-coffee investigation, and the logs must be able to answer it.

### VI. Personal-Data Privacy (NON-NEGOTIABLE)

- `profile.md`, `screening_prompt.md`, `registry.toml`, and `jobs.db` contain personal
  data. They MUST never be committed, and their contents MUST never be quoted in
  specs, plans, PRs, commit messages, or agent output.
- Secrets (API keys, connection strings) MUST live in environment variables locally
  and in the platform secret store in Azure — never in code, config files in git, or
  CI logs.
- Cloud resources holding personal data (the data store, runtime files) MUST NOT be
  publicly accessible.

Rationale: the repository is public-remote; the personal data that makes the agent
useful is exactly the data that must never leave the runtime environment.

### VII. LLM Spend Efficiency (NON-NEGOTIABLE)

Any pipeline stage that calls the LLM (today, scoring) MUST treat model tokens as the
scarcest resource and spend them only on judgments a cheaper mechanism cannot make.
This principle concretizes Principle I's requirement that LLM spend be bounded.

- **Filter before you spend.** Deterministic, zero-cost gates (target buckets / title
  keywords, location, seniority, posting age, salary floor) MUST run before the LLM,
  so the model only judges plausible matches. The LLM is for nuanced judgment, never
  for first-pass elimination of obviously-irrelevant rows. The concrete filter
  criteria are defined per feature via `/speckit-specify` and tuned through runtime
  files (Principle IV), not hardcoded.
- **Cache the static prefix.** Content identical across calls within a run — the
  candidate profile and the screening prompt — MUST use prompt caching
  (`cache_control`) rather than being re-sent on every batch.
- **Budget guardrail.** Each run MUST enforce a configurable cap — maximum postings
  scored and/or maximum estimated dollars per run — and stop loudly (Principle V)
  rather than exceed it. This is the enforceable form of Principle I's "upper bound on
  LLM calls."
- **Cost observability.** Each run MUST log its per-run token usage and estimated cost
  so the Anthropic bill (Principle I's dual-bill monitoring) can be reconciled from
  logs without re-running. Caps and pricing inputs are environment-configurable,
  consistent with config-via-env-var and fail-loud conventions.

Rationale: a fresh board — or a newly added company — presents its entire backlog as
unscored at once, so a single cold-start can cost more than a month of steady-state
digests. A spend ceiling alone reacts after the money is gone; efficiency mechanics
(pre-filtering, caching, a hard per-run cap) keep the bill proportionate to the value
delivered, before the call is made.

## Operational Constraints

- **Language/tooling**: Python ≥ 3.12, `uv` for dependency and script management,
  `pytest` for tests, `hatchling` for builds.
- **Source of truth**: https://github.com/ctwarrick/job-agent; `main` is the only
  long-lived branch and the only deployment source. Infrastructure is defined as code
  in this repository (Principle II).
- **Hosting**: a single Azure tenant (tenant ID supplied as deployment configuration,
  never committed).
- **Storage**: postings and application state live in a single durable store that the
  scheduled run reads and writes. The concrete storage service is deliberately NOT
  specified here; it is selected at plan time against Azure's capabilities and
  Principles I and IV. SQLite (`jobs.db`) remains the local development store.
- **Schedule**: the digest targets delivery at 06:00 America/Los_Angeles by default;
  both the time and the timezone are configuration, not code.
- **Data retention**: posting data is retained for two months by default, then
  purged. The window is configurable and will be reassessed against real usage.
- **Runtime tuning**: scoring behavior is adjusted by editing `profile.md` and
  `screening_prompt.md`, never by code changes; in production these are updated via
  the private manual mechanism sanctioned in Principle II.

## Development Workflow & Quality Gates

- Features flow through Spec Kit: constitution → `/speckit-specify` →
  `/speckit-clarify` → `/speckit-plan` → `/speckit-tasks` → `/speckit-implement`,
  layered on the multi-agent workflow defined in `AGENTS.md` (scout → planner →
  test-writer → implementer → reviewer).
- The plan-phase Constitution Check gates every feature against these principles,
  with explicit attention to the cost ceiling (I), infrastructure-as-code (II), and
  simplicity (IV).
- Division of control: the agent fleet implements; the maintainer approves plans,
  decides when work is complete, and is the only actor who pushes to `main`. From
  there, GitHub Actions owns test-and-deploy (Principle III).
- The reviewer independently re-runs `uv run pytest`; an implementer's green claim is
  never trusted unverified.

## Governance

- This constitution supersedes other practice documents. `AGENTS.md` provides
  operational detail for agents; where the two conflict, the constitution wins.
- Amendments are made by the sole maintainer via a commit that updates this file,
  bumps the version per semantic versioning (MAJOR: principle removal or
  redefinition; MINOR: new principle or materially expanded guidance; PATCH:
  clarification or wording), and refreshes the Sync Impact Report.
- Compliance is reviewed at two points: the plan-phase Constitution Check and the
  independent review before commit. Deviations MUST be justified in the plan's
  Complexity Tracking table or rejected.

**Version**: 1.1.0 | **Ratified**: 2026-06-10 | **Last Amended**: 2026-06-12
