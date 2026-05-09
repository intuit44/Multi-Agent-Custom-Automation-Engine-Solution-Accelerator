#!/bin/bash

# Deploy Strategic Autonomy Infrastructure
# This script creates:
# 1. Azure Service Bus Namespace
# 2. Service Bus Topic (strategic-decisions)
# 3. Service Bus Subscription (critical-failure-handler)
# 4. Azure Function Apps (Timer + Consumer)
# 5. Configure environment variables

set -e

# Configuration
SUBSCRIPTION_ID="${AZURE_SUBSCRIPTION_ID:-380fa841-83f3-42fe-adc4-582a5ebe139b}"
RESOURCE_GROUP="${RESOURCE_GROUP:-boat-rental-app-group}"
LOCATION="${LOCATION:-eastus2}"
SERVICE_BUS_NAMESPACE="fibroskin-servicebus"
SERVICE_BUS_TOPIC="strategic-decisions"
SERVICE_BUS_SUBSCRIPTION="critical-failure-handler"
STORAGE_ACCOUNT="fibroskinautomationstg"
TIMER_FUNCTION_APP="fibroskin-strategic-timer"
CONSUMER_FUNCTION_APP="fibroskin-strategic-consumer"

echo "==============================================="
echo "Strategic Autonomy Infrastructure Deployment"
echo "==============================================="
echo "Subscription: $SUBSCRIPTION_ID"
echo "Resource Group: $RESOURCE_GROUP"
echo "Location: $LOCATION"
echo ""

# Set subscription
echo "[1/6] Setting Azure subscription..."
az account set --subscription "$SUBSCRIPTION_ID" || {
    echo "ERROR: Failed to set subscription. Please check your Azure credentials."
    exit 1
}
echo "✓ Subscription set"

# Create Service Bus Namespace
echo ""
echo "[2/6] Creating Service Bus Namespace..."
az servicebus namespace create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$SERVICE_BUS_NAMESPACE" \
    --location "$LOCATION" \
    --sku Standard \
    --output none || {
    echo "Note: Service Bus Namespace may already exist"
}
echo "✓ Service Bus Namespace ready: $SERVICE_BUS_NAMESPACE"

# Create Topic
echo ""
echo "[3/6] Creating Service Bus Topic..."
az servicebus topic create \
    --resource-group "$RESOURCE_GROUP" \
    --namespace-name "$SERVICE_BUS_NAMESPACE" \
    --name "$SERVICE_BUS_TOPIC" \
    --default-message-time-to-live PT1H \
    --output none || {
    echo "Note: Topic may already exist"
}
echo "✓ Service Bus Topic ready: $SERVICE_BUS_TOPIC"

# Create Subscription
echo ""
echo "[4/6] Creating Service Bus Subscription..."
az servicebus topic subscription create \
    --resource-group "$RESOURCE_GROUP" \
    --namespace-name "$SERVICE_BUS_NAMESPACE" \
    --topic-name "$SERVICE_BUS_TOPIC" \
    --name "$SERVICE_BUS_SUBSCRIPTION" \
    --max-delivery-count 3 \
    --output none || {
    echo "Note: Subscription may already exist"
}
echo "✓ Service Bus Subscription ready: $SERVICE_BUS_SUBSCRIPTION"

# Get Connection String
echo ""
echo "[5/6] Retrieving Service Bus Connection String..."
CONNECTION_STRING=$(az servicebus namespace authorization-rule keys list \
    --resource-group "$RESOURCE_GROUP" \
    --namespace-name "$SERVICE_BUS_NAMESPACE" \
    --name RootManageSharedAccessKey \
    --query primaryConnectionString -o tsv)

if [ -z "$CONNECTION_STRING" ]; then
    echo "ERROR: Failed to retrieve connection string"
    exit 1
fi
echo "✓ Connection String retrieved (length: ${#CONNECTION_STRING})"

# Update .env file
echo ""
echo "[6/6] Updating .env file with Service Bus configuration..."
ENV_FILE="src/backend/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: .env file not found at $ENV_FILE"
    exit 1
fi

# Backup .env
cp "$ENV_FILE" "${ENV_FILE}.backup"
echo "✓ Backup created: ${ENV_FILE}.backup"

# Update or add AZURE_SERVICE_BUS_CONNECTION_STRING
if grep -q "^AZURE_SERVICE_BUS_CONNECTION_STRING=" "$ENV_FILE"; then
    # Replace existing (escape special chars in connection string)
    sed -i "s|^AZURE_SERVICE_BUS_CONNECTION_STRING=.*|AZURE_SERVICE_BUS_CONNECTION_STRING=$CONNECTION_STRING|" "$ENV_FILE"
else
    # Add new line after other Service Bus vars
    sed -i "/^AZURE_SERVICE_BUS_TOPIC=/a AZURE_SERVICE_BUS_CONNECTION_STRING=$CONNECTION_STRING" "$ENV_FILE"
fi
echo "✓ .env updated with connection string"

# Display summary
echo ""
echo "==============================================="
echo "✓ Strategic Autonomy Infrastructure Ready"
echo "==============================================="
echo ""
echo "Service Bus Details:"
echo "  Namespace: $SERVICE_BUS_NAMESPACE"
echo "  Topic: $SERVICE_BUS_TOPIC"
echo "  Subscription: $SERVICE_BUS_SUBSCRIPTION"
echo "  Connection String: [CONFIGURED]"
echo ""
echo "Next Steps:"
echo "1. Deploy Timer Function:"
echo "   cd src/backend"
echo "   func azure functionapp publish $TIMER_FUNCTION_APP --build remote"
echo ""
echo "2. Deploy Consumer Function:"
echo "   func azure functionapp publish $CONSUMER_FUNCTION_APP --build remote"
echo ""
echo "3. Configure Function App Settings (via Azure Portal or CLI):"
echo "   For Timer Function:"
echo "     - STRATEGIC_TIMER_SCHEDULE=0 */15 * * * *"
echo "     - ACCELERATOR_API_BASE_URL=https://<your-api>.azurewebsites.net"
echo "     - ACCELERATOR_API_BEARER_TOKEN=<optional>"
echo ""
echo "   For Consumer Function:"
echo "     - STRATEGIC_ACTION_WEBHOOK_URL=<optional>"
echo "     - STRATEGIC_ACTION_WEBHOOK_BEARER_TOKEN=<optional>"
echo ""
echo "4. Test the integration:"
echo "   curl -X POST 'http://localhost:8000/api/strategic/analyze?force_publish=true'"
echo ""
