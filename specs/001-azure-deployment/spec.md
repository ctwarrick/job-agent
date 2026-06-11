# Feature Specification: Cloud-Native Scheduled Operation on Azure

**Feature Branch**: `001-azure-deployment`

**Created**: 2026-06-10

**Status**: Draft

**Input**: User description: "Run job-agent as a cloud-native application on Azure:
it runs daily overnight so a triaged digest email is in the maintainer's inbox first
thing in the morning, with no dependency on a local machine. On every push to main,
tests run first and the application deploys to Azure only on green. Costs stay under
the constitutional ceiling; infrastructure is reproducible from the repository."

## Clarifications

### Session 2026-06-11

- Q: When the overnight run fails for a transient reason, should the system retry
  automatically before alerting? → A: Up to 2 automatic retries (spaced ~15–30
  minutes apart) before the 06:00 deadline; alert only if the final attempt also
  fails.
- Q: Should the retention purge apply to all postings equally, or spare postings
  with application activity? → A: Purge only postings with status `new` or
  `dismissed`; postings with any application activity are kept indefinitely.
- Q: Should the maintainer be able to trigger a production run on demand? → A: Yes —
  a documented on-demand trigger, usable for recovery and after runtime-file
  updates.
- Q: Where should failure alerts reach the maintainer? → A: Both a platform-sent
  alert email and an SMS message (delivery paths independent of the application's
  own email sending).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Morning digest arrives unattended (Priority: P1)

Every morning, the maintainer opens their inbox over coffee and finds the day's
triaged job digest waiting, delivered by 06:00 Pacific. No machine was left on
overnight, no button was pressed, and nothing needed restarting. On days when no new
postings qualify, a short "no new matches today" email arrives instead, so the
presence of an email always confirms the system ran.

**Why this priority**: This is the entire reason the product exists. Every other
story supports this one.

**Independent Test**: Schedule the pipeline in the cloud against a small registry,
wait for the scheduled time, and confirm the digest email arrives by 06:00 Pacific
with correctly triaged content — with the maintainer's local machine powered off.

**Acceptance Scenarios**:

1. **Given** the system is deployed and qualifying postings exist, **When** the
   scheduled overnight run completes, **Then** a digest email listing those postings
   is in the maintainer's inbox by 06:00 America/Los_Angeles.
2. **Given** the system is deployed and no new postings qualify, **When** the
   scheduled run completes, **Then** a "no new matches" email arrives by the same
   deadline.
3. **Given** a posting appeared in yesterday's digest, **When** today's run executes,
   **Then** that posting is not repeated (sent-state survives between runs).
4. **Given** the schedule or timezone configuration is changed, **When** the next run
   fires, **Then** delivery follows the new schedule with no code change.

---

### User Story 2 - Push to main ships to production (Priority: P2)

The maintainer finishes a piece of work locally (built by the agent fleet, approved
by the maintainer), pushes to `main`, and walks away. The test suite runs
automatically; if it passes, the new version is live in Azure before the next
overnight run. If any test fails, nothing deploys and the previous version keeps
running.

**Why this priority**: This is how every future change — including the remaining
stories — reaches production safely. Without it, deployment is manual toil.

**Independent Test**: Push a trivially observable change to `main` and confirm tests
ran first and the change is live in production without further human action; push a
deliberately failing test and confirm deployment is blocked.

**Acceptance Scenarios**:

1. **Given** a commit on `main` with a green test suite, **When** the push completes,
   **Then** the new version is deployed to production with no human action after the
   push.
2. **Given** a commit on `main` with a failing test, **When** the pipeline runs,
   **Then** deployment is blocked, the previous version remains in service, and the
   failure is visible to the maintainer.
3. **Given** secrets configured for deployment, **When** any pipeline run executes,
   **Then** no secret value appears in logs or build output.

---

### User Story 3 - Failures are visible by morning coffee (Priority: P3)

One morning the digest email is missing. Within moments, the maintainer finds a
failure notification on a channel that doesn't depend on the digest email path,
opens the run's logs, and identifies what broke — without re-running anything. On a
different morning the digest arrives but notes that one job board was unreachable;
the rest of the digest is intact.

**Why this priority**: The system runs while its only operator sleeps. Undetected
silent failure erodes the product's core promise; a missing email must be
self-explanatory by breakfast.

**Independent Test**: Force a hard failure (e.g., invalid credentials) and a partial
failure (one unreachable source) in a controlled run; confirm the hard failure
raises a non-email notification with diagnosable logs, and the partial failure still
delivers a digest that reports the degradation.

**Acceptance Scenarios**:

1. **Given** one ATS board is down, **When** the overnight run executes, **Then** the
   digest still arrives containing results from the healthy sources plus a visible
   notice of which source failed.
2. **Given** a hard failure (missing configuration, storage unavailable, email send
   failure), **When** the run aborts, **Then** a failure signal reaches the
   maintainer through a channel independent of the digest email.
3. **Given** any failed or degraded run, **When** the maintainer reviews its logs the
   next morning, **Then** the cause is identifiable without re-running the pipeline.
4. **Given** a failed night whose cause has been fixed, **When** the maintainer
   invokes the documented on-demand trigger, **Then** a full run executes and the
   digest is delivered without waiting for the next scheduled run.

---

### User Story 4 - Updating the personal files in production (Priority: P4)

The maintainer edits their profile or screening criteria locally, then pushes the
updated personal files to production through a private, documented mechanism — the
single sanctioned manual operation. The next overnight run scores postings against
the updated criteria. These files never pass through the public repository.

**Why this priority**: Occasional maintenance; required for the scorer to evolve
with the job search, but infrequent compared with daily operation.

**Independent Test**: Update a runtime file in production via the documented
mechanism, trigger a run, and confirm the new criteria took effect; verify the files
are not publicly accessible and absent from the repository.

**Acceptance Scenarios**:

1. **Given** an updated `profile.md`, **When** it is placed in production via the
   documented private mechanism, **Then** the next run uses the new content.
2. **Given** the production storage of personal files, **When** access is attempted
   without the maintainer's credentials, **Then** access is denied.

---

### User Story 5 - Rebuild everything from the repository (Priority: P5)

Worst case: the Azure environment is lost or broken beyond repair. Following the
repository's documented procedure — applying the infrastructure definitions, adding
secrets and the personal runtime files — the maintainer recreates the entire
production environment, and the next overnight run proceeds as if nothing happened.

**Why this priority**: Rare event, but it is the safety net that makes a
single-person operation sustainable, and it is constitutionally required.

**Independent Test**: From a clean state (or separate resource group), provision the
environment using only repository contents plus secrets and runtime files; confirm a
scheduled run completes end-to-end.

**Acceptance Scenarios**:

1. **Given** only the repository, the secrets, and the personal runtime files,
   **When** the documented provisioning procedure is followed, **Then** a fully
   functional production environment exists with no portal-only configuration steps.
2. **Given** a provisioned environment, **When** infrastructure drift is suspected,
   **Then** re-applying the repository's infrastructure definition restores the
   declared state.

### Edge Cases

- **Zero qualifying postings**: an empty-day notice email is sent, so a missing email
  always means failure (never "nothing matched").
- **LLM scoring unavailable mid-run** (outage/quota): unscored postings remain queued
  for the next run; the digest still goes out with already-scored matches and notes
  the degradation.
- **Email delivery itself fails**: the run aborts loud; the failure signal still
  arrives via the platform-sent alert email and SMS, whose delivery paths do not
  share the application's email dependency.
- **Daylight-saving transitions**: delivery tracks 06:00 in the configured timezone,
  not a fixed UTC offset.
- **Transient overnight failure**: a failed run retries automatically (up to 2
  attempts, ~15–30 minutes apart) before the delivery deadline; the
  independent-channel alert fires only when the final attempt fails.
- **Overlapping runs**: if a run is still executing when the next would start, only
  one run executes; state is never corrupted by concurrency.
- **Runaway LLM usage**: a defect (e.g., accidentally un-scoring the whole store)
  cannot exceed the per-run bound on scoring calls.
- **Spend approaching the ceiling**: the maintainer is alerted before the $50/month
  all-in ceiling is breached, covering both cloud and LLM billing.
- **Aged data**: untouched postings (status `new` or `dismissed`) older than the
  retention window (default two months) are purged on a regular cadence; postings
  with application activity are never purged.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST execute the full pipeline (fetch → score → digest)
  automatically on a daily schedule, with no dependency on any local machine or
  manual trigger.
- **FR-002**: System MUST deliver the digest email by 06:00 America/Los_Angeles by
  default; the delivery time and timezone MUST be configurable without code changes.
- **FR-003**: System MUST send an email on every successful scheduled run — a digest
  when matches exist, a "no new matches" notice otherwise — so that the absence of
  an email always indicates failure.
- **FR-004**: System MUST persist all pipeline state (postings, scores, application
  status, sent-tracking) durably across runs; dedupe and "already emailed" behavior
  MUST survive between executions.
- **FR-005**: System MUST treat per-source failures as non-fatal: when an individual
  job board is unreachable, the run continues, the digest is delivered from healthy
  sources, and the digest visibly reports the degraded source.
- **FR-006**: System MUST treat configuration, storage, and email-delivery failures
  as fatal: abort without sending a partial or stale digest, and once automatic
  retries (FR-018) are exhausted, emit a failure signal via both a platform-sent
  alert email and an SMS message — delivery paths independent of the application's
  own email sending.
- **FR-007**: System MUST retain logs for each run, accessible after the fact,
  sufficient to diagnose a failed or degraded run without interactive re-execution.
- **FR-008**: Every push to `main` MUST run the full test suite first and deploy to
  production only when all tests pass; a failing suite MUST block deployment with no
  override path.
- **FR-009**: Deployment after a green test run MUST complete without any human
  action beyond the push itself.
- **FR-010**: All production infrastructure MUST be declared as code in the
  repository; the production environment MUST be reproducible from the repository
  plus secrets plus the personal runtime files, with no portal-only steps.
- **FR-011**: Secrets MUST be stored in the platform's secret store and supplied to
  the application at runtime; no secret value may appear in the repository, the
  infrastructure definitions, or CI logs.
- **FR-012**: Personal runtime files (`profile.md`, `screening_prompt.md`,
  `registry.txt`) MUST be updatable in production through a private, documented,
  manual mechanism; they MUST never enter the repository and MUST NOT be publicly
  accessible.
- **FR-013**: System MUST enforce a configurable per-run upper bound on LLM scoring
  calls.
- **FR-014**: System MUST provide spend visibility against the $50/month all-in
  ceiling, alerting the maintainer before the ceiling is breached, across both cloud
  and LLM billing.
- **FR-015**: System MUST purge postings older than a configurable retention window
  (default: two months) only when their application status is `new` or `dismissed`;
  postings with any other application activity are retained indefinitely.
- **FR-016**: System MUST NOT incur always-on compute charges; compute resources are
  consumed only while a run or deployment is executing.
- **FR-017**: System MUST ensure at most one pipeline run executes at a time.
- **FR-018**: System MUST automatically retry a failed overnight run up to 2 times,
  spaced approximately 15–30 minutes apart, with all attempts completing before the
  configured delivery deadline; the failure alert fires only if the final attempt
  fails.
- **FR-019**: System MUST provide a documented on-demand trigger that lets the
  maintainer — and only the maintainer — start a full pipeline run outside the
  schedule, for recovery after a failed night or verification after runtime-file
  updates. Concurrency protection (FR-017) applies to manual runs as well.

### Key Entities

- **Posting**: a normalized job posting (title, company, location, URL, scores,
  rationale, sent-tracking); deduplicated by fingerprint; subject to retention.
- **Application record**: maintainer-facing status per posting (new, dismissed,
  applied…); joins postings to digest eligibility.
- **Run**: one scheduled or manual execution of the pipeline, with a start time,
  outcome (success / degraded / failed), and retrievable logs.
- **Personal runtime files**: `profile.md`, `screening_prompt.md`, `registry.txt` —
  private inputs delivered out-of-band, never via the repository.
- **Infrastructure definition**: the versioned declaration of all production
  resources, living in the repository.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For 30 consecutive days, an email (digest or no-matches notice) is in
  the maintainer's inbox by 06:00 Pacific every morning with zero manual
  interventions.
- **SC-002**: Total monthly spend across all providers stays at or under $50,
  verifiable on both bills.
- **SC-003**: A change pushed to `main` is live in production within 15 minutes,
  with no human action after the push.
- **SC-004**: Any failed overnight run produces both a platform-sent alert email
  and an SMS to the maintainer by 06:30 the same morning, delivered even when the
  application's own email sending is broken.
- **SC-005**: The maintainer can diagnose any failed or degraded run from retained
  logs alone, without re-running the pipeline.
- **SC-006**: A complete production environment can be recreated from the
  repository, secrets, and runtime files in under one hour following the documented
  procedure.
- **SC-007**: The maintainer can always distinguish "no matching jobs today" from
  "the system is broken" from the inbox alone.

## Assumptions

- The application pipeline is already functional end-to-end locally, including email
  delivery of the digest; this feature changes *where and how* it runs, with
  application-side changes limited to cloud-operation needs (empty-day notice,
  per-source degradation, LLM call bound, retention purge, storage location
  configurability).
- Production starts with an empty data store: historical postings, scores, and
  dismissals in the local development database are not migrated. Postings re-fetch
  naturally; prior dismissal history is accepted as lost.
- Sender and recipient email addresses are supplied via configuration, not recorded
  in the repository or this spec.
- The Azure tenant ID is supplied as deployment-time configuration by the
  maintainer and is never committed.
- A single production environment is sufficient: no staging environment, and no
  multi-user concerns — the maintainer is the only user (solo-developer project per
  the constitution).
- The overnight run's start time is chosen during planning so that fetch, scoring,
  and delivery reliably complete before the 06:00 Pacific delivery target.
- The company registry remains small (tens of companies), so a complete run fits
  comfortably within ordinary scheduled-job time limits.
- The concrete storage service, email delivery mechanism, and infrastructure
  tooling are deliberately deferred to the planning phase, per the constitution's
  Operational Constraints. (The alert channels are decided: platform alert email
  plus SMS.)
- Approaching the spend ceiling triggers alerts only; the system never halts itself
  automatically. A small, bounded overshoot is preferred over a silently missing
  digest, and ceiling changes remain a maintainer decision per the constitution.

## Out of Scope

- New ATS adapters or changes to fetching/scoring logic beyond what cloud operation
  requires.
- Redesign of digest content or formatting.
- Migration of historical data from the local development database.
- A staging environment, preview deployments, or multi-user capabilities.
- Changes to the local development workflow (local runs continue to work as today).
- Post-application outcome feedback: feeding application results (e.g., rejections,
  interviews reached) back into the LLM scoring analysis. Flagged as a promising
  future feature during clarification — deliberately excluded from this MVP, whose
  priority is getting the application running in Azure.
