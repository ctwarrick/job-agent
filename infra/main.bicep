// infra/main.bicep — core MVP infrastructure for the job-agent pipeline.
//
// Resources (per specs/001-azure-deployment/contracts/deployment.md, US1 scope):
//   - Log Analytics workspace          : run logs (FR-007), Container Apps env sink
//   - Container Apps environment       : Consumption plan, hosts the job
//   - Storage account + Files share    : jobs.db + runtime files (no public access)
//   - Key Vault (standard)             : secret store (FR-011)
//   - Container Apps Job               : Schedule trigger, the pipeline itself
//
// Deliberately OUT OF SCOPE for this file (later user stories / polish):
//   - Action group / alert rules (US3, T036)
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

@description('Cron schedule (UTC) for the Container Apps Job, three ticks ~20 min apart.')
param cronExpression string = '0,20,40 11 * * *'

@description('IANA timezone passed through to the app as JOBAGENT_TZ for digest-date computation.')
param tz string = 'America/Los_Angeles'

@description('Per-run cap on LLM scoring batches, passed through as JOBAGENT_MAX_LLM_CALLS.')
param maxLlmCalls int = 10

@description('Retention window in days for unloved postings, passed through as JOBAGENT_RETENTION_DAYS.')
param retentionDays int = 60

@description('Container image tag (e.g. git SHA) for ghcr.io/<owner>/job-agent.')
param imageTag string = 'latest'

@description('Container image repository reference, without tag.')
param imageRepository string = 'ghcr.io/ctwarrick/job-agent'

@description('Replica timeout in seconds for the job (~900s per contract).')
param replicaTimeoutSeconds int = 900

@description('Scoring model passed through as JOBAGENT_MODEL; the cost dial.')
param model string = 'claude-sonnet-4-6'

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
              name: 'JOBAGENT_MAX_LLM_CALLS'
              value: string(maxLlmCalls)
            }
            {
              name: 'JOBAGENT_RETENTION_DAYS'
              value: string(retentionDays)
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
// Outputs
// ---------------------------------------------------------------------------

output resourceGroupName string = resourceGroup().name
output containerAppsEnvironmentName string = containerAppsEnv.name
output jobName string = job.name
output storageAccountName string = storage.name
output fileShareName string = dataShare.name
output keyVaultName string = keyVault.name
output logAnalyticsWorkspaceName string = logAnalytics.name
