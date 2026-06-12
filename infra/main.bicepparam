// infra/main.bicepparam — non-secret defaults for infra/main.bicep.
//
// Per contracts/deployment.md: alertEmail / smsCountryCode / smsPhone are
// personal data and are NEVER committed — they are absent from this file
// entirely and supplied at deploy time (CLI --parameters for the bootstrap
// deploy; repo secrets ALERT_EMAIL/SMS_COUNTRY_CODE/SMS_PHONE for CI deploys
// once US3's alerting lands).
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
