#!/bin/bash

# Script para asignar permisos necesarios a la identidad

OBJECT_ID="3fc6e3d7-7197-4a3d-b285-5ccff482f7ef"
SUBSCRIPTION_ID="${AZURE_AI_SUBSCRIPTION_ID:-380fa841-83f3-42fe-adc4-582a5ebe139b}"
RESOURCE_GROUP="${AZURE_AI_RESOURCE_GROUP:-boat-rental-app-group}"
WORKSPACE_NAME="${AZURE_AI_PROJECT_NAME}"
KEY_VAULT_NAME="yellowstkeyvault8df0efc3"

echo "Asignando rol 'AzureML Data Scientist' a la identidad..."

az role assignment create \
  --assignee-object-id "$OBJECT_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "AzureML Data Scientist" \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.MachineLearningServices/workspaces/$WORKSPACE_NAME"

echo "Asignando rol 'Key Vault Secrets Officer' a la identidad..."

az role assignment create \
  --assignee-object-id "$OBJECT_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets Officer" \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.KeyVault/vaults/$KEY_VAULT_NAME"

echo "Permisos asignados correctamente"
