#!/usr/bin/env bash
#
# bootstrap.sh — one-time-per-environment setup for job-agent on Azure.
#
# Creates the resource group and the GitHub-OIDC deploy identity that
# `infra/main.bicep` deployments and the (future) `deploy.yml` workflow
# authenticate as. Everything else (Container Apps environment, the job,
# storage, Key Vault, ...) is declared in `infra/main.bicep` and created by
# `az deployment group create` — see docs/manual-deployment.md.
#
# Per contracts/deployment.md "Bootstrap contract":
#   - create the resource group
#   - create a user-assigned managed identity (the GitHub-OIDC deploy identity)
#   - add a federated credential trusting
#     repo:<owner>/job-agent:ref:refs/heads/main
#   - grant that identity Contributor + role-assignment rights, scoped to the
#     resource group only (identity & access matrix, FR-022)
#
# It also registers the resource providers infra/main.bicep depends on, so a
# fresh subscription does not fail the first deployment on an unregistered
# provider (Microsoft.Insights did exactly that on the first real deploy).
# Provider registration is subscription-wide and idempotent, so it is safe to
# re-run and belongs in this one-time-per-environment setup.
#
# Inputs (env vars or flags — never hardcoded; nothing here is committed):
#   AZURE_SUBSCRIPTION_ID   subscription to deploy into
#   AZURE_TENANT_ID         tenant for the federated credential issuer
#   GITHUB_REPO             "<owner>/job-agent" slug used in the federated
#                           credential subject
#   RESOURCE_GROUP          resource group name (default: jobagent-rg)
#   LOCATION                resource group location (default: westus2)
#   IDENTITY_NAME           name for the deploy UAMI (default: jobagent-deploy)
#
# Usage:
#   AZURE_SUBSCRIPTION_ID=... AZURE_TENANT_ID=... GITHUB_REPO=owner/job-agent \
#     ./scripts/bootstrap.sh
#
# Equivalent positional form:
#   ./scripts/bootstrap.sh <subscription-id> <tenant-id> <owner/job-agent> \
#       [resource-group] [location] [identity-name]
#
# Idempotent: re-running against an existing resource group / identity /
# federated credential is a no-op for resources that already exist.

set -euo pipefail

SUBSCRIPTION_ID="${1:-${AZURE_SUBSCRIPTION_ID:-}}"
TENANT_ID="${2:-${AZURE_TENANT_ID:-}}"
GITHUB_REPO="${3:-${GITHUB_REPO:-}}"
RESOURCE_GROUP="${4:-${RESOURCE_GROUP:-jobagent-rg}}"
LOCATION="${5:-${LOCATION:-westus2}}"
IDENTITY_NAME="${6:-${IDENTITY_NAME:-jobagent-deploy}}"

if [[ -z "$SUBSCRIPTION_ID" || -z "$TENANT_ID" || -z "$GITHUB_REPO" ]]; then
  echo "Usage: AZURE_SUBSCRIPTION_ID=... AZURE_TENANT_ID=... GITHUB_REPO=<owner>/job-agent $0" >&2
  echo "       (or positional: $0 <subscription-id> <tenant-id> <owner>/job-agent [resource-group] [location] [identity-name])" >&2
  exit 1
fi

echo "Subscription:    $SUBSCRIPTION_ID"
echo "Tenant:          $TENANT_ID"
echo "GitHub repo:     $GITHUB_REPO"
echo "Resource group:  $RESOURCE_GROUP ($LOCATION)"
echo "Deploy identity: $IDENTITY_NAME"

az account set --subscription "$SUBSCRIPTION_ID"

# Register the resource providers infra/main.bicep declares resources under. A
# fresh subscription may have some unregistered, which fails the deployment
# (Microsoft.Insights bit the first real deploy). --wait blocks until each is
# Registered so a deploy right after bootstrap can't race registration.
# Microsoft.Authorization (RBAC) is always registered and is omitted on purpose.
echo "Registering resource providers used by infra/main.bicep..."
for ns in \
  Microsoft.App \
  Microsoft.Consumption \
  Microsoft.Insights \
  Microsoft.KeyVault \
  Microsoft.ManagedIdentity \
  Microsoft.OperationalInsights \
  Microsoft.Storage; do
  echo "  registering $ns..."
  az provider register --namespace "$ns" --wait
done

echo "Creating resource group..."
az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --output none

echo "Creating user-assigned managed identity for GitHub-OIDC deploys..."
az identity create \
  --name "$IDENTITY_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --output none

IDENTITY_PRINCIPAL_ID=$(az identity show \
  --name "$IDENTITY_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query principalId -o tsv)

IDENTITY_CLIENT_ID=$(az identity show \
  --name "$IDENTITY_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query clientId -o tsv)

echo "Adding federated credential trusting repo:${GITHUB_REPO}:ref:refs/heads/main..."
if az identity federated-credential show \
    --name "github-main" \
    --identity-name "$IDENTITY_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --output none 2>/dev/null; then
  echo "  already exists, skipping"
else
  az identity federated-credential create \
    --name "github-main" \
    --identity-name "$IDENTITY_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --issuer "https://token.actions.githubusercontent.com" \
    --subject "repo:${GITHUB_REPO}:ref:refs/heads/main" \
    --audiences "api://AzureADTokenExchange" \
    --output none
fi

RG_SCOPE="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}"

echo "Granting Contributor on the resource group..."
if az role assignment list \
    --assignee "$IDENTITY_PRINCIPAL_ID" \
    --role "Contributor" \
    --scope "$RG_SCOPE" \
    --output tsv | grep -q .; then
  echo "  already granted, skipping"
else
  az role assignment create \
    --assignee-object-id "$IDENTITY_PRINCIPAL_ID" \
    --assignee-principal-type ServicePrincipal \
    --role "Contributor" \
    --scope "$RG_SCOPE" \
    --output none
fi

echo "Granting role-assignment rights on the resource group (User Access Administrator)..."
if az role assignment list \
    --assignee "$IDENTITY_PRINCIPAL_ID" \
    --role "User Access Administrator" \
    --scope "$RG_SCOPE" \
    --output tsv | grep -q .; then
  echo "  already granted, skipping"
else
  az role assignment create \
    --assignee-object-id "$IDENTITY_PRINCIPAL_ID" \
    --assignee-principal-type ServicePrincipal \
    --role "User Access Administrator" \
    --scope "$RG_SCOPE" \
    --output none
fi

cat <<EOF

Bootstrap complete.

  Resource group:        $RESOURCE_GROUP
  Deploy identity name:  $IDENTITY_NAME
  Deploy identity client ID: $IDENTITY_CLIENT_ID
  Tenant ID:             $TENANT_ID
  Subscription ID:       $SUBSCRIPTION_ID

For US2 (CI deploys), record the client ID, tenant ID, and subscription ID as
GitHub Actions repository secrets/variables for OIDC login. Until then, an
authenticated maintainer can deploy infra/main.bicep manually with
'az deployment group create' — see docs/manual-deployment.md.
EOF
