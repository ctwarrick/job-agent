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
// DEVIATION from tasks.md T023: budgetAmount is intentionally omitted. The
// Microsoft.Consumption/budgets resource is Polish-phase (T043), not part of
// this MVP's main.bicep, so there is no corresponding parameter to set yet.

using 'main.bicep'

param location = 'westus2'
param namePrefix = 'jobagent'
param cronExpression = '0,20,40 11 * * *'
param tz = 'America/Los_Angeles'
param maxPostingsPerRun = 200
param maxCostPerRun = '5.00'
param retentionDays = 60

// Secret action-group receivers — values read from the environment at compile
// time, never committed (see header). No default: a missing variable fails the
// compile loudly rather than silently building a receiver-less alert.
param alertEmail = readEnvironmentVariable('ALERT_EMAIL')
param smsCountryCode = readEnvironmentVariable('SMS_COUNTRY_CODE')
param smsPhone = readEnvironmentVariable('SMS_PHONE')
