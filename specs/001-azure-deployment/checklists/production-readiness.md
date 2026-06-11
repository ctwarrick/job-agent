# Production Readiness Checklist: Cloud-Native Scheduled Operation on Azure

**Purpose**: Validate that reliability/operations, deployment/IaC, and security/privacy requirements are complete, clear, consistent, and measurable before `/speckit-tasks` — and serve as a reviewer reference during implementation.
**Created**: 2026-06-11
**Resolved**: 2026-06-11 — all 26 items closed via spec/contract edits (commit pending)
**Feature**: [spec.md](../spec.md) | [plan.md](../plan.md) | [contracts/](../contracts/)
**Audience**: Maintainer (pre-tasks gate) and reviewer agent (during build)

**Note**: This checklist tests the quality of the *requirements*, not the implementation. Each item asks whether the requirement is well-specified — not whether the system works. Resolution notes (→) name the edit or the consciously accepted position.

## Requirement Completeness

- [x] CHK001 - Is mid-run LLM-outage degradation stated as a testable functional requirement? [Gap, Spec §Edge Cases vs §FR-005/FR-006]
  → Promoted to new **FR-020** (LLM unavailability and cap exhaustion are non-fatal degradation; digest still delivered, degradation reported).
- [x] CHK002 - Are enforcement requirements specified so FR-008's "no override path" is verifiable? [Gap, Spec §FR-008]
  → FR-008 now defines "no override" (no manual-approval bypass, no skip-tests trigger, no continue-on-error); deployment.md CI contract bans `workflow_dispatch` on build/deploy. Maintainer bootstrap deploys outside CI explicitly exempted.
- [x] CHK003 - Are requirements defined for a deployment failing partway through the CI chain? [Gap, Exception Flow]
  → New spec edge case "deployment fails after tests pass": previous version stays in service; GitHub's run-failure notification is the (distinct) signal. Mirrored in deployment.md CI contract.
- [x] CHK004 - Are duplicate-digest semantics specified for the send-vs-commit crash window? [Gap, Edge Case, Spec §FR-004/FR-018]
  → Decided **at-least-once**: new spec edge case; runtime-config.md marker-ordering note; data-model.md commit-after-confirmed-send rule. Duplicate accepted over missing.
- [x] CHK005 - Are concurrency requirements defined for a manual start during an in-flight run? [Gap, Spec §FR-017/FR-019]
  → data-model.md startup check now has an explicit in-flight lock (NULL outcome within replica timeout → no-op; stale NULL = crashed, marked failed); `JOBAGENT_FORCE` does not bypass it (runtime-config.md, deployment.md). Spec edge case extended to "scheduled or manual".
- [x] CHK006 - Is the alert drill a stated requirement with a cadence? [Gap, Spec §SC-004]
  → runtime-config.md: drill **required** at initial provisioning/rebuild and after any marker/query change. Quickstart bootstrap says so explicitly.
- [x] CHK007 - Is the supply path for never-committed parameters on routine CI redeploys specified? [Gap, deployment.md]
  → deployment.md "Never-committed parameters" section: CI passes `ALERT_EMAIL`/`SMS_COUNTRY_CODE`/`SMS_PHONE` from GitHub Actions secrets on every deploy; GH secrets count as "secrets" for FR-010. Quickstart adds the set-secrets bootstrap step.

## Requirement Clarity & Measurability

- [x] CHK008 - Does FR-002's "configurable without code changes" define the acceptable mechanism? [Ambiguity, Spec §FR-002]
  → FR-002 now reads "without **application** code changes"; infrastructure parameter change + redeploy explicitly qualifies.
- [x] CHK009 - Is SC-006's one-hour rebuild bound scoped? [Measurability, Spec §SC-006]
  → SC-006 now defines the clock: bootstrap start (credentials/secrets/files at hand) → completed on-demand run delivering a digest. Quickstart step 8 is the stop condition.
- [x] CHK010 - Is an at-ship acceptance criterion defined for SC-001? [Measurability, Spec §SC-001]
  → SC-001 now names the ship-time evidence (one unattended on-time run + passed alert drill + verified on-demand trigger); 30 days is post-ship monitoring.
- [x] CHK011 - Is FR-007's "sufficient to diagnose" mapped to enumerated minimum log contents for every fatal class? [Clarity, Spec §FR-007]
  → runtime-config.md enumerates per-run minimums: stage reached, per-source outcomes with error text, counts, and on fatal failure the stage + exception + config key/resource involved.
- [x] CHK012 - Can SC-004's "alert by 06:30" be demonstrated from the alert parameters? [Measurability, Spec §SC-004]
  → Yes under the redesigned rule (see CHK014): deadline-gated query + 30-min evaluation ⇒ worst case deadline + 30 min = 06:30; local-time evaluation is DST-correct. Derivation in deployment.md alert row.
- [x] CHK013 - Are retry spacing and the before-deadline bound reconciled with the cron mechanism? [Clarity, Consistency, Spec §FR-018]
  → FR-018 allows a fixed scheduler interval within the 15–30 min window and includes max run duration in the deadline bound. deployment.md adds the arithmetic: 20-min spacing > 900 s timeout (no scheduled overlap); last attempt done ~04:55 PDT worst case.

## Requirement Consistency & Conflicts

- [x] CHK014 - Can `RUN_SUCCESS` from no-op/manual runs mask a failed overnight run? [Conflict, runtime-config.md, Spec §SC-004]
  → **Alert redesigned**: pure 25-h absence query replaced by a missed-deadline query keyed on the current local `digest_date` (fires past the deadline only if no success marker carries *that day's* date). Updated in deployment.md, runtime-config.md, plan.md summary; quickstart caveat reworded; step 8 seeds day-one success.
- [x] CHK015 - Does committed `alertEmail` conflict with "no email addresses in the repository"? [Conflict, deployment.md, Spec §Assumptions]
  → `alertEmail` reclassified as never-committed alongside the SMS parameters; spec assumption extended to alert receivers; CI supplies them per CHK007.
- [x] CHK016 - Does the Anthropic hard spend limit conflict with the alerts-only/never-halt assumption? [Conflict, deployment.md, Spec §Assumptions]
  → Changed to a spend **notification**, explicitly not a hard limit, in deployment.md step 3 and quickstart step 6; rationale recorded (FR-013 is the runaway protection).
- [x] CHK017 - Is the scoring-model default bump reconciled with Out of Scope? [Conflict, plan.md, Spec §Out of Scope]
  → New spec assumption: bump is forced by provider retirement of the old default; a configuration-default change under cost discipline, not scoring logic; score drift across the change accepted.
- [x] CHK018 - Are "single sanctioned manual operation" and the three post-deploy manual steps reconciled? [Conflict, Constitution §II, deployment.md]
  → deployment.md classification paragraph: steps 1–3 + bootstrap = one-time provisioning (re-done on rebuild/rotation); the runtime-file upload is the single *recurring* sanctioned operation. No constitution change needed.

## Security & Privacy Coverage

- [x] CHK019 - Is the public image traced to a spec-level requirement with content criteria? [Traceability, Gap, Constitution §VI]
  → New **FR-021**: artifact may be public, must contain only public-repo code/config; personal files, state, and secrets arrive only at runtime.
- [x] CHK020 - Are least-privilege access scopes defined per identity? [Coverage, Gap, deployment.md]
  → New **FR-022** + deployment.md "Identity & access matrix" enumerating deploy UAMI, job MI, maintainer, action group — including the consciously accepted broad spots (Contributor implies key-listing; UAMI technically holds job-start).
- [x] CHK021 - Are log-content privacy requirements defined against Principle VI? [Coverage, Spec §FR-007]
  → FR-007 extended: no secret values, runtime-file contents, or scoring rationale in logs/alert payloads; registry company names in per-source status accepted because the log store is private. Mirrored in runtime-config.md log minimums.
- [x] CHK022 - Is "no secret value in CI logs" verifiable? [Measurability, Spec §FR-011, deployment.md]
  → deployment.md "Secret hygiene" defines what counts as a secret value (keys, SMTP creds, OIDC tokens, KV values — names are public), the masking mechanism, and the no-echo/no-`--debug` rules.
- [x] CHK023 - Is FR-019's "only the maintainer" traceable to a stated authorization rule, including US4 share access? [Traceability, Spec §FR-019/US4]
  → FR-019 now requires a stated access-control rule; deployment.md ties the trigger to `microsoft.app/jobs/start/action` held only by the maintainer, and the identity matrix covers share/KV access (no identity outside the table).

## Dependencies & Assumptions

- [x] CHK024 - Is the SMTP-egress-from-ACA assumption validated or flagged? [Assumption, Spec §Assumptions]
  → New spec assumption: outbound SMTP assumed permitted, verified by the provisioning validation run (quickstart step 8); fallback decision deferred until disproven.
- [x] CHK025 - Is the run-duration budget traced to the replica timeout with stated consequences? [Assumption, deployment.md]
  → deployment.md cron-derivation constraints: spacing must exceed `replicaTimeout`; a run exceeding the timeout is killed, marked Failed, and retried/alerted. Registry-size assumption unchanged in spec.
- [x] CHK026 - Are first-run requirements under the empty-store assumption specified? [Assumption, Edge Case, Spec §FR-013]
  → Spec empty-store assumption extended: backlog scores down under the cap with degradation noted, or maintainer backfills via on-demand trigger with a raised bound; deployment.md shows the backfill command.

## Notes

- All 26 items resolved on 2026-06-11 by edits to `spec.md` (FR-002/007/008/018/019 clarified; FR-020/021/022 added; 3 edge cases; SC-001/SC-006 scoped; 4 assumptions), `contracts/deployment.md`, `contracts/runtime-config.md`, `data-model.md`, `plan.md` (summary wording), and `quickstart.md` (bootstrap steps 2/6/8, drill required, caveat).
- The five [Conflict] items were resolved without amending the constitution; CHK018 is a classification, not a rule change.
- Largest design consequence: the failure alert is now a **missed-deadline, digest_date-keyed** query rather than a 25-hour absence window (CHK012/CHK014) — `infra/main.bicep`'s alert rule and the log-marker contract must be implemented to the new semantics.
