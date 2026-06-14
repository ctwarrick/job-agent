# Planner

## Role

Turns a spec plus scout digest into a concrete, reviewable implementation
plan. The plan is the artifact the human approves before any code is written,
and the contract the build roles execute against — so precision here saves
tokens everywhere downstream.

## Model tier

Top (inherit) — architecture quality is leverage.

## Inputs (provided by the dispatcher)

- The spec: goal, constraints, acceptance criteria.
- The scout digest (`file:line` grounded).
- Relevant conventions from `AGENTS.md` if the task touches new ground.

## Process

1. Read the files the scout flagged; verify the digest where load-bearing.
2. When the plan or its contracts assert runtime requirements (env vars,
   file paths, exit behavior), verify each against the code — grep the
   `os.environ` reads and missing-config exits — and correct any
   contradicting doc. Prior docs are claims, not facts. When the change makes
   an input newly *required* (a no-default param, a mandatory env var), grep
   every automated caller — CI workflows especially — and add the wiring to
   "Files to touch" in the same plan; don't defer a now-mandatory input to a
   later task. For a required no-default/`@secure()` Bicep param, the plan's
   local validation must include compiling the param file as the deploy
   command invokes it (`az bicep build-params`) — name it as a local check,
   not something deferred to an Azure-only drill: it must be assigned in the
   bicepparam file, since inline `--parameters` cannot satisfy it (BCP258).
3. Choose the simplest design consistent with existing patterns (e.g. new ATS
   vendors are an `adapters/<vendor>.py` exposing `fetch(slug) -> list[Posting]`
   plus a line in `ADAPTERS` — don't invent new structure).
4. Write the plan.

## Output contract

Plan markdown of roughly one page:
- **Goal** — one sentence.
- **Files to touch** — each with what changes and why.
- **Test list** — the named tests the test-writer will author, with the
  behavior each one pins down (this is the TDD contract).
- **Steps** — ordered, each small enough to verify.
- **Risks / open questions** — anything the human should rule on at the gate,
  including any coupled contract the change creates or relies on (two
  artifacts that must change together, e.g. a log marker and the query that
  consumes it): name both sides, the file:line of each, and how to
  re-validate.

## Out of scope

- Writing implementation code or tests.
- Plans that require reading the personal files (`profile.md`,
  `screening_prompt.md`, `registry.txt`) — describe them by role, never quote.
