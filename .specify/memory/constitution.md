<!--
Sync Impact Report
- Version change: 1.0.0 (draft) → 1.0.0 (draft amended prior to ratification; no bump —
  semantic versioning applies from first ratification)
- Modified principles:
  - I. Cost Discipline — added $50/month all-in ceiling across providers, dual-bill
    monitoring (Azure + Anthropic), per-run LLM call bound, maintainer-approved override
  - II. Cloud-Native Scheduled Operation — added infrastructure-as-code requirement;
    carved out the manual, private update path for personal runtime files; delivery
    target now a configurable 06:00 America/Los_Angeles default
  - III. Test-First Delivery — clarified solo-dev flow: agents implement, the maintainer
    alone pushes to main, GitHub Actions tests then deploys
  - V. Fail Loud, Fail Visibly — split configuration failures (fail hard) from transient
    per-source failures (degrade, deliver, report); failure alerts must not depend on
    the digest email path; logging sufficient for morning-after diagnosis
- Modified sections:
  - Operational Constraints — production storage method deliberately unspecified
    (selected at plan time); schedule default 06:00 America/Los_Angeles, configurable;
    two-month data-retention default, configurable
  - Development Workflow & Quality Gates — division of control between agent fleet,
    maintainer, and CI made explicit
- Added sections: none
- Removed sections: none
- Templates:
  - ✅ .specify/templates/plan-template.md — compatible; Constitution Check should test
    the cost ceiling (I), IaC (II), and simplicity (IV)
  - ✅ .specify/templates/spec-template.md — no constitution-dependent content
  - ✅ .specify/templates/tasks-template.md — no constitution-dependent content
  - ✅ AGENTS.md / CLAUDE.md — consistent; no edits required
- Follow-up TODOs:
  - Azure tenant ID remains deployment configuration; never committed.
  - Production storage service and the runtime-file delivery mechanism are deliberate
    open questions for /speckit-plan.
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
  budget.
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
  `registry.txt` cannot travel through git (Principle VI), so they are updated in
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

- `profile.md`, `screening_prompt.md`, `registry.txt`, and `jobs.db` contain personal
  data. They MUST never be committed, and their contents MUST never be quoted in
  specs, plans, PRs, commit messages, or agent output.
- Secrets (API keys, connection strings) MUST live in environment variables locally
  and in the platform secret store in Azure — never in code, config files in git, or
  CI logs.
- Cloud resources holding personal data (the data store, runtime files) MUST NOT be
  publicly accessible.

Rationale: the repository is public-remote; the personal data that makes the agent
useful is exactly the data that must never leave the runtime environment.

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

**Version**: 1.0.0 | **Ratified**: 2026-06-10 | **Last Amended**: 2026-06-10
