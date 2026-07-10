// infra/main.bicep — core MVP infrastructure for the job-agent pipeline.
//
// Resources (per specs/001-azure-deployment/contracts/deployment.md, US1 + US3
// alerting scope):
//   - Log Analytics workspace          : run logs (FR-007), Container Apps env sink
//   - Container Apps environment       : Consumption plan, hosts the job
//   - Storage account + Files share    : jobs.db + runtime files (no public access)
//   - Key Vault (standard)             : secret store (FR-011)
//   - Container Apps Job               : Schedule trigger, the pipeline itself
//   - Action group                     : email + SMS alert receivers (US3, FR-006)
//   - Scheduled query alert rule       : missed-deadline page by ~06:30 (US3, SC-004)
//
// Deliberately OUT OF SCOPE for this file (later user stories / polish):
//   - Microsoft.Consumption/budgets (Polish, T043)
//   - GitHub-OIDC deploy user-assigned identity + federated credential (US2,
//     T026 — created by scripts/bootstrap.sh, referenced here only later)
//
// Secret-valued env vars are Key Vault references resolved by the job's
// user-assigned managed identity (granted Key Vault Secrets User below,
// before the job is created so the references resolve on first deploy);
// non-secret env vars are plain values from params (contracts/runtime-config.md).

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Naming seed for all resources in this deployment. Must start with a letter; lowercase letters, digits, and hyphens only (Key Vault names must begin with a letter).')
param namePrefix string = 'jobagent'

// Storage account and Key Vault names must be lowercase, and Key Vault names
// have a 24-char limit. A long or uppercase namePrefix could otherwise
// produce an invalid storage account name or let take(...,24) swallow the
// uniqueString suffix entirely, losing global uniqueness. Lowercase the
// prefix and cap it at 7 chars so the 4-char infix ('stor'/'-kv-') plus the
// full 13-char uniqueString suffix always fit in 24.
var sanitizedPrefix = take(toLower(replace(namePrefix, '-', '')), 7)

@description('Cron schedule (UTC) for the Container Apps Job, three ticks after local midnight (R2).')
param cronExpression string = '0 8,10,12 * * *'

@description('IANA timezone passed through to the app as JOBAGENT_TZ for digest-date computation.')
param tz string = 'America/Los_Angeles'

@description('Per-run cap on postings scored before a clean stop, as JOBAGENT_MAX_POSTINGS_PER_RUN.')
param maxPostingsPerRun int = 200

@description('Per-run estimated USD spend cap before a clean stop, as JOBAGENT_MAX_COST_PER_RUN (string: Bicep has no float).')
param maxCostPerRun string = '5.00'

@description('Per-MTok input price for the cost cap + SCORE_SUMMARY estimate, as JOBAGENT_PRICE_INPUT (claude-sonnet-4-6 rate).')
param priceInput string = '3'

@description('Per-MTok output price, as JOBAGENT_PRICE_OUTPUT (claude-sonnet-4-6 rate).')
param priceOutput string = '15'

@description('Per-MTok cache-write price, as JOBAGENT_PRICE_CACHE_WRITE (claude-sonnet-4-6 rate).')
param priceCacheWrite string = '3.75'

@description('Per-MTok cache-read price, as JOBAGENT_PRICE_CACHE_READ (claude-sonnet-4-6 rate).')
param priceCacheRead string = '0.30'

@description('Retention window in days for postings, passed through as JOBAGENT_RETENTION_DAYS.')
param retentionDays int = 60

@description('Container image tag (e.g. git SHA) for ghcr.io/<owner>/job-agent.')
param imageTag string = 'latest'

@description('Container image repository reference, without tag.')
param imageRepository string = 'ghcr.io/ctwarrick/job-agent'

@description('Replica timeout in seconds for the job (2h overnight window, US1 007-overnight-scale).')
param replicaTimeoutSeconds int = 7200

@description('Scoring model passed through as JOBAGENT_MODEL; the cost dial.')
param model string = 'claude-sonnet-4-6'

// US3 alerting params (contracts/deployment.md → Never-committed parameters).
// alertEmail / smsCountryCode / smsPhone are personal data: they have NO
// defaults (a forgotten value fails the deploy loud rather than building a
// receiver-less alert) and are @secure() so Azure keeps them out of deployment
// history and CI --debug output (FR-007). Supplied on every deploy — CLI
// --parameters for the bootstrap deploy, repo secrets for CI.

@secure()
@description('Action-group email receiver. Supplied at deploy time; never committed.')
param alertEmail string

@secure()
@description('Action-group SMS receiver country code (e.g. "1"). Supplied at deploy time; never committed.')
param smsCountryCode string

@secure()
@description('Action-group SMS receiver phone number. Supplied at deploy time; never committed.')
param smsPhone string

@description('Action-group short name shown as the SMS/email sender prefix (<=12 chars). Human-facing; kept separate from the resource-name prefix.')
@maxLength(12)
param alertShortName string = 'JobAgent'

@description('Local delivery deadline hour (0-23) in tz; the missed-deadline alert evaluates as missed once local time passes this hour with no RUN_SUCCESS for the day. A deadline change is a redeploy, not a query edit (FR-002).')
param deliveryDeadlineHourLocal int = 6

@description('Monthly cloud spend ceiling in account currency for the cost budget; alerts fire at 50% and 80% of this. The $50 figure is the constitutional all-in ceiling (FR-014, SC-002); the Anthropic/LLM half is a separate console budget per quickstart.md Bootstrap.')
param budgetAmount int = 50

@description('Cost-budget period start. IMMUTABLE once the budget exists — Azure rejects any deploy that changes it ("Start date of budgets cannot be updated"), so an existing environment must pin this in main.bicepparam to the date the budget was created with. The utcNow default (first of the current UTC month, per the Consumption first-of-month creation rule) is safe only for the FIRST deploy into a fresh environment. Both rules are enforced at deploy, not by az bicep build.')
param budgetStartDate string = '${utcNow('yyyy-MM')}-01T00:00:00Z'

// ---------------------------------------------------------------------------
// Log Analytics workspace — run logs (FR-007) and Container Apps env sink
// ---------------------------------------------------------------------------

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-law'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

// ---------------------------------------------------------------------------
// Storage account + Azure Files share — jobs.db + runtime files
// No public blob or file share access (FR-012, US4).
// ---------------------------------------------------------------------------

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: take('${sanitizedPrefix}stor${uniqueString(resourceGroup().id)}', 24)
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
    }
  }
}

resource fileServices 'Microsoft.Storage/storageAccounts/fileServices@2023-01-01' = {
  parent: storage
  name: 'default'
}

resource dataShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  parent: fileServices
  name: '${namePrefix}-data'
  properties: {
    enabledProtocols: 'SMB'
    shareQuota: 5
  }
}

// ---------------------------------------------------------------------------
// Key Vault (standard) — secret store (FR-011)
// ---------------------------------------------------------------------------

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: take('${sanitizedPrefix}-kv-${uniqueString(resourceGroup().id)}', 24)
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

// ---------------------------------------------------------------------------
// Container Apps environment (Consumption) — wired to Log Analytics
// ---------------------------------------------------------------------------

resource containerAppsEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// Mount the Azure Files share into the Container Apps environment so the job
// can reference it as a volume.
resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: containerAppsEnv
  name: '${namePrefix}-data'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: dataShare.name
      accessMode: 'ReadWrite'
    }
  }
}

// ---------------------------------------------------------------------------
// User-assigned managed identity for the job runtime, and its Key Vault
// Secrets User role assignment (identity & access matrix, FR-022).
//
// Created and granted BEFORE the job so that the job's Key Vault secret
// references (which ACA validates at create/update) resolve on first
// deploy — see docs/manual-deployment.md for the two-pass deploy flow.
// This is distinct from the GitHub-OIDC deploy identity (out of scope here).
// ---------------------------------------------------------------------------

resource jobIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-job-identity'
  location: location
}

@description('Built-in role definition ID for "Key Vault Secrets User".')
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource jobKeyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, jobIdentity.id, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: jobIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// GitHub-OIDC deploy identity (T026, FR-022) — REFERENCE ONLY.
//
// This user-assigned identity and its federated credential `github-main`
// (subject `repo:<owner>/job-agent:ref:refs/heads/main`) are created out-of-band
// by scripts/bootstrap.sh, NOT by this template. CI authenticates *as* this
// identity to run `az deployment group create`, so the deployment cannot be what
// first creates its own login (chicken-and-egg) — and the federated credential
// must exist before any workflow can obtain a token at all. Declaring it
// `existing` records it in the IaC (contracts/deployment.md resource inventory:
// "created by bootstrap, referenced here") and surfaces its client ID. A deploy
// run before bootstrap fails loud right here, which enforces the documented
// order. The identity's RG-scoped Contributor + User Access Administrator grant
// is owned by bootstrap.sh and enumerated in the identity & access matrix.
// ---------------------------------------------------------------------------

@description('Name of the GitHub-OIDC deploy identity created by scripts/bootstrap.sh (its IDENTITY_NAME default is "<namePrefix>-deploy").')
param deployIdentityName string = '${namePrefix}-deploy'

resource deployIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: deployIdentityName
}

// ---------------------------------------------------------------------------
// Container Apps Job — the pipeline (Schedule trigger)
// ---------------------------------------------------------------------------

resource job 'Microsoft.App/jobs@2024-03-01' = {
  name: '${namePrefix}-job'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${jobIdentity.id}': {}
    }
  }
  dependsOn: [
    jobKeyVaultSecretsUser
  ]
  properties: {
    environmentId: containerAppsEnv.id
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: replicaTimeoutSeconds
      replicaRetryLimit: 0
      scheduleTriggerConfig: {
        cronExpression: cronExpression
        parallelism: 1
        replicaCompletionCount: 1
      }
      secrets: [
        {
          name: 'anthropic-api-key'
          keyVaultUrl: '${keyVault.properties.vaultUri}secrets/anthropic-api-key'
          identity: jobIdentity.id
        }
        {
          name: 'smtp-host'
          keyVaultUrl: '${keyVault.properties.vaultUri}secrets/smtp-host'
          identity: jobIdentity.id
        }
        {
          name: 'smtp-port'
          keyVaultUrl: '${keyVault.properties.vaultUri}secrets/smtp-port'
          identity: jobIdentity.id
        }
        {
          name: 'smtp-user'
          keyVaultUrl: '${keyVault.properties.vaultUri}secrets/smtp-user'
          identity: jobIdentity.id
        }
        {
          name: 'smtp-pass'
          keyVaultUrl: '${keyVault.properties.vaultUri}secrets/smtp-pass'
          identity: jobIdentity.id
        }
        {
          name: 'digest-to'
          keyVaultUrl: '${keyVault.properties.vaultUri}secrets/digest-to'
          identity: jobIdentity.id
        }
        {
          name: 'salary-floor'
          keyVaultUrl: '${keyVault.properties.vaultUri}secrets/salary-floor'
          identity: jobIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'job-agent'
          image: '${imageRepository}:${imageTag}'
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: [
            {
              name: 'ANTHROPIC_API_KEY'
              secretRef: 'anthropic-api-key'
            }
            {
              name: 'SMTP_HOST'
              secretRef: 'smtp-host'
            }
            {
              name: 'SMTP_PORT'
              secretRef: 'smtp-port'
            }
            {
              name: 'SMTP_USER'
              secretRef: 'smtp-user'
            }
            {
              name: 'SMTP_PASS'
              secretRef: 'smtp-pass'
            }
            {
              name: 'DIGEST_TO'
              secretRef: 'digest-to'
            }
            {
              name: 'JOBAGENT_SALARY_FLOOR'
              secretRef: 'salary-floor'
            }
            {
              name: 'PYTHONUNBUFFERED'
              value: '1'
            }
            {
              name: 'JOBAGENT_DATA_DIR'
              value: '/data'
            }
            {
              name: 'JOBAGENT_TZ'
              value: tz
            }
            {
              name: 'JOBAGENT_MODEL'
              value: model
            }
            {
              name: 'JOBAGENT_MAX_POSTINGS_PER_RUN'
              value: string(maxPostingsPerRun)
            }
            {
              name: 'JOBAGENT_MAX_COST_PER_RUN'
              value: maxCostPerRun
            }
            {
              name: 'JOBAGENT_PRICE_INPUT'
              value: priceInput
            }
            {
              name: 'JOBAGENT_PRICE_OUTPUT'
              value: priceOutput
            }
            {
              name: 'JOBAGENT_PRICE_CACHE_WRITE'
              value: priceCacheWrite
            }
            {
              name: 'JOBAGENT_PRICE_CACHE_READ'
              value: priceCacheRead
            }
            {
              name: 'JOBAGENT_RETENTION_DAYS'
              value: string(retentionDays)
            }
            {
              // Single source of truth: the app validates against the exact
              // deadline the platform enforces (contracts/runtime-config.md).
              name: 'JOBAGENT_EXECUTION_WINDOW_SECONDS'
              value: string(replicaTimeoutSeconds)
            }
          ]
          volumeMounts: [
            {
              volumeName: 'data'
              mountPath: '/data'
            }
          ]
        }
      ]
      volumes: [
        {
          name: 'data'
          storageType: 'AzureFile'
          storageName: envStorage.name
        }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Action group — email + SMS alert receivers (US3, FR-006)
// Outbound notification only; holds no role (identity & access matrix).
// ---------------------------------------------------------------------------

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: '${namePrefix}-alerts'
  location: 'global'
  properties: {
    groupShortName: alertShortName
    enabled: true
    emailReceivers: [
      {
        name: 'maintainer-email'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
    smsReceivers: [
      {
        name: 'maintainer-sms'
        countryCode: smsCountryCode
        phoneNumber: smsPhone
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Scheduled query alert rule — missed-deadline page (US3, SC-004)
//
// ⚠️ COUPLED CONTRACT (contracts/runtime-config.md log-marker contract): the
// query below keys on the literal `RUN_SUCCESS digest_date=<today>` printed by
// main.py. Changing the marker format OR this query is a breaking change that
// must edit both in the same commit and be re-validated by the T037 alert drill.
//
// Semantics: every 30 min, compute "now" in the deployment timezone
// (DST-correct via datetime_utc_to_local). Once local time is past
// deliveryDeadlineHourLocal AND no RUN_SUCCESS marker carries today's local
// date, the query returns one row -> the rule fires -> action group (email +
// SMS). Keying on today's date stops evening manual runs or no-op skips
// (older/different digest_date) from masking a failed overnight run. windowSize
// P1D so each intra-day evaluation still sees the day's early (~03:00-04:00
// local) success marker. autoMitigate self-clears on a recovery run or at local
// midnight (date key rolls -> pastDeadline false -> zero rows -> resolved).
// Worst case notify = deadline + one 30-min evaluation ~= 06:30, SC-004's bound.
//
// skipQueryValidation: ContainerAppConsoleLogs_CL does not exist until the job
// has logged at least once, so deploy-time validation would fail on a fresh
// workspace (the first deploy provisions LAW and this rule together). The query
// is exercised for real by the required T037 drill instead.
// ---------------------------------------------------------------------------

// Built with join() over interpolated lines, not a '''multi-line''' literal:
// Bicep triple-quoted strings are verbatim and would emit `${tz}` literally.
var missedDeadlineQuery = join([
  'let tzName = "${tz}";'
  'let deadlineHour = ${deliveryDeadlineHourLocal};'
  'let localNow = datetime_utc_to_local(now(), tzName);'
  'let localMidnight = startofday(localNow);'
  'let todayKey = format_datetime(localMidnight, "yyyy-MM-dd");'
  'let pastDeadline = localNow >= localMidnight + (deadlineHour * 1h);'
  'ContainerAppConsoleLogs_CL'
  '| where TimeGenerated > ago(1d)'
  '| where Log_s contains_cs strcat("RUN_SUCCESS digest_date=", todayKey)'
  '| summarize successMarkers = count()'
  '| where pastDeadline and successMarkers == 0'
], '\n')

var missedDeadlineDescription = join([
  'Fires when no RUN_SUCCESS is logged for today by the local deadline - the'
  'overnight pipeline delivered no digest. Action: check the Container App'
  'job\'s latest run and logs. Auto-resolves on a recovery run or at local'
  'midnight; a Resolved notification means the pipeline recovered and no'
  'action is needed.'
], ' ')

resource missedDeadlineAlert 'Microsoft.Insights/scheduledQueryRules@2022-06-15' = {
  name: '${namePrefix}-missed-deadline'
  location: location
  kind: 'LogAlert'
  properties: {
    displayName: '${alertShortName}: morning digest missed its deadline'
    description: missedDeadlineDescription
    severity: 1
    enabled: true
    scopes: [
      logAnalytics.id
    ]
    evaluationFrequency: 'PT30M'
    windowSize: 'P1D'
    autoMitigate: true
    skipQueryValidation: true
    criteria: {
      allOf: [
        {
          query: missedDeadlineQuery
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Monthly cost budget — spend visibility (FR-014, SC-002)
//
// Scoped to this resource group (the deployment scope), so it tracks the job's
// cloud spend. Alerts at 50% and 80% of budgetAmount through the SAME action
// group as the missed-deadline page, so a spend warning reaches the maintainer
// by email + SMS with no second channel to wire. This covers the cloud half of
// the $50 all-in ceiling; the Anthropic/LLM half is a separate console budget,
// the documented manual step in quickstart.md Bootstrap.
//
// startDate must be the first of a month per the Consumption rule, and is
// IMMUTABLE after creation — a redeploy whose date differs from the existing
// budget's fails with "Start date of budgets cannot be updated". The utcNow
// default is therefore creation-only; existing environments pin the original
// date in main.bicepparam. Both rules are enforced at DEPLOY, not by
// `az bicep build` — a bad date fails at deploy, not validate-infra.sh.
// ---------------------------------------------------------------------------

resource budget 'Microsoft.Consumption/budgets@2023-05-01' = {
  name: '${namePrefix}-monthly'
  properties: {
    category: 'Cost'
    amount: budgetAmount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: budgetStartDate
    }
    // contactGroups reuses the action group (email + SMS); contactEmails is
    // marked required by the Bicep budget type (a known type inaccuracy:
    // contactGroups alone satisfies the API), so alertEmail is repeated here to
    // keep the deploy warning-free and unambiguously valid. A budget threshold
    // crossing may therefore double-send email (action group + direct) — rare
    // and accepted over a possibly-rejected empty contactEmails array.
    notifications: {
      Actual_GreaterThan_50_Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 50
        thresholdType: 'Actual'
        contactEmails: [
          alertEmail
        ]
        contactGroups: [
          actionGroup.id
        ]
      }
      Actual_GreaterThan_80_Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: [
          alertEmail
        ]
        contactGroups: [
          actionGroup.id
        ]
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

output resourceGroupName string = resourceGroup().name
output containerAppsEnvironmentName string = containerAppsEnv.name
output jobName string = job.name
output storageAccountName string = storage.name
output fileShareName string = dataShare.name

// Record as the AZURE_CLIENT_ID GitHub Actions repo secret for CI OIDC login
// (a client ID is an identifier, not a credential). Resolving this also fails
// the deploy loudly if scripts/bootstrap.sh has not yet created the identity.
output deployIdentityClientId string = deployIdentity.properties.clientId
output keyVaultName string = keyVault.name
output logAnalyticsWorkspaceName string = logAnalytics.name
output actionGroupName string = actionGroup.name
output missedDeadlineAlertName string = missedDeadlineAlert.name
output budgetName string = budget.name
