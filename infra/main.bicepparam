// infra/main.bicepparam — non-secret defaults for infra/main.bicep.
//
// Per contracts/deployment.md: alertEmail / smsCountryCode / smsPhone are
// personal data and their VALUES are NEVER committed. They are assigned below
// from environment variables (readEnvironmentVariable, evaluated at compile
// time), so this file references only the variable NAMES — the values live in
// the deploy environment: repo secrets ALERT_EMAIL / SMS_COUNTRY_CODE /
// SMS_PHONE for CI deploys, the maintainer's shell for a bootstrap deploy.
// A .bicepparam file is compiled before any CLI --parameters merge, so a
// required (no-default) param MUST be assigned in this file or the compile
// fails BCP258 — inline --parameters cannot satisfy it. Reading from the
// environment satisfies BCP258 without committing a value, and a missing
// variable fails the compile loudly (BCP427) rather than building a
// receiver-less alert.
//
// budgetAmount is the cloud-side monthly ceiling for the Microsoft.Consumption
// budget (T043). budgetStartDate is PINNED to the date the live budget was
// created with (2026-06-01): Azure forbids changing a budget's start date
// after creation, so the template's utcNow default would break every redeploy
// in a later month. When bootstrapping a fresh environment, set this to the
// first of that month (or delete the stale budget first).

using 'main.bicep'

param location = 'westus2'
param namePrefix = 'jobagent'
// Three attempts at 08:00/10:00/12:00 UTC, 2h apart == the 2h
// replicaTimeoutSeconds (7200s) window: the exact boundary is backstopped by
// the existing in-flight startup-check no-op (research R2), not by spacing
// headroom. All three fire after local midnight in both PST and PDT (keeping
// the run-start digest_date correct year-round; research R2, resolved).
// Supersedes the live v2.4.2 stopgap (`0 10,11,12`, 2700s).
param cronExpression = '0 8,10,12 * * *'
// 2-hour overnight window (007-overnight-scale US1): the enlarged
// (Workday-heavy) registry's full sequential fetch plus score/digest
// headroom needs more than the prior 45-min stopgap. The startup coherence
// check (FR-004) fails loud if the fetch budget + headroom can't fit this.
param replicaTimeoutSeconds = 7200
param tz = 'America/Los_Angeles'
param maxPostingsPerRun = 200
param maxCostPerRun = '5.00'
param retentionDays = 60
param budgetAmount = 50
param budgetStartDate = '2026-06-01T00:00:00Z'

// Secret action-group receivers — values read from the environment at compile
// time, never committed (see header). No default: a missing variable fails the
// compile loudly rather than silently building a receiver-less alert.
param alertEmail = readEnvironmentVariable('ALERT_EMAIL')
param smsCountryCode = readEnvironmentVariable('SMS_COUNTRY_CODE')
param smsPhone = readEnvironmentVariable('SMS_PHONE')
