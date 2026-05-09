#!/bin/bash

# List of Azure regions to check for quota (update as needed)
IFS=', ' read -ra REGIONS <<< "$AZURE_REGIONS"

SUBSCRIPTION_ID="${AZURE_SUBSCRIPTION_ID}"
GPT_MIN_CAPACITY="${GPT_MIN_CAPACITY}"
O4_MINI_MIN_CAPACITY="${O4_MINI_MIN_CAPACITY}"
GPT41_MINI_MIN_CAPACITY="${GPT41_MINI_MIN_CAPACITY}"

echo "🔄 Validating required environment variables..."
if [[ -z "$SUBSCRIPTION_ID" || -z "$REGIONS" ]]; then
    echo "❌ ERROR: Missing required environment variables."
    echo "Required: AZURE_SUBSCRIPTION_ID, AZURE_REGIONS"
    echo "Optional: O4_MINI_MIN_CAPACITY (default: 50), GPT41_MINI_MIN_CAPACITY (default: 50)"
    exit 1
fi

echo "🔄 Setting Azure subscription..."
if ! az account set --subscription "$SUBSCRIPTION_ID"; then
    echo "❌ ERROR: Invalid subscription ID or insufficient permissions."
    exit 1
fi
echo "✅ Azure subscription set successfully."

# ── Container App Environments quota check ────────────────────────────────────
# Azure allows at most 5 Container App Environments per subscription globally.
# Check the current count before attempting deployment so we can fail fast with
# a clear message rather than getting a mid-deployment ARM error.
MAX_CAE_ENVIRONMENTS=5
echo "🔄 Checking Container App Environments quota (max: ${MAX_CAE_ENVIRONMENTS})..."

CAE_COUNT=$(az rest \
  --method get \
  --url "https://management.azure.com/subscriptions/${SUBSCRIPTION_ID}/providers/Microsoft.App/managedEnvironments?api-version=2024-03-01" \
  --query "length(value)" \
  --output tsv 2>/dev/null || echo "0")

CAE_COUNT=${CAE_COUNT:-0}
echo "📊 Container App Environments in use: ${CAE_COUNT} / ${MAX_CAE_ENVIRONMENTS}"

if [ "${CAE_COUNT}" -ge "${MAX_CAE_ENVIRONMENTS}" ]; then
    echo "❌ ERROR: Subscription has reached the limit of ${MAX_CAE_ENVIRONMENTS} Container App Environments (currently: ${CAE_COUNT}). Deployment would fail with MaxNumberOfGlobalEnvironmentsInSubExceeded."
    echo "QUOTA_FAILED=true" >> "$GITHUB_ENV"
    exit 0
fi
echo "✅ Container App Environments quota check passed (${CAE_COUNT}/${MAX_CAE_ENVIRONMENTS} in use)."
# ── End Container App Environments quota check ───────────────────────────────

# Define models and their minimum required capacities
declare -A MIN_CAPACITY=(
    ["OpenAI.GlobalStandard.o4-mini"]="${O4_MINI_MIN_CAPACITY}"
    ["OpenAI.GlobalStandard.gpt4.1"]="${GPT_MIN_CAPACITY}"
    ["OpenAI.GlobalStandard.gpt4.1-mini"]="${GPT41_MINI_MIN_CAPACITY}"
)

VALID_REGION=""
for REGION in "${REGIONS[@]}"; do
    echo "----------------------------------------"
    echo "🔍 Checking region: $REGION"

    QUOTA_INFO=$(az cognitiveservices usage list --location "$REGION" --output json)
    if [ -z "$QUOTA_INFO" ]; then
        echo "⚠️ WARNING: Failed to retrieve quota for region $REGION. Skipping."
        continue
    fi

    INSUFFICIENT_QUOTA=false
    for MODEL in "${!MIN_CAPACITY[@]}"; do
        MODEL_INFO=$(echo "$QUOTA_INFO" | awk -v model="\"value\": \"$MODEL\"" '
            BEGIN { RS="},"; FS="," }
            $0 ~ model { print $0 }
        ')

        if [ -z "$MODEL_INFO" ]; then
            echo "⚠️ WARNING: No quota information found for model: $MODEL in $REGION. Skipping."
            INSUFFICIENT_QUOTA=true
            continue
        fi

        CURRENT_VALUE=$(echo "$MODEL_INFO" | awk -F': ' '/"currentValue"/ {print $2}' | tr -d ',' | tr -d ' ')
        LIMIT=$(echo "$MODEL_INFO" | awk -F': ' '/"limit"/ {print $2}' | tr -d ',' | tr -d ' ')

        CURRENT_VALUE=${CURRENT_VALUE:-0}
        LIMIT=${LIMIT:-0}

        CURRENT_VALUE=$(echo "$CURRENT_VALUE" | cut -d'.' -f1)
        LIMIT=$(echo "$LIMIT" | cut -d'.' -f1)

        AVAILABLE=$((LIMIT - CURRENT_VALUE))

        echo "✅ Model: $MODEL | Used: $CURRENT_VALUE | Limit: $LIMIT | Available: $AVAILABLE"

        if [ "$AVAILABLE" -lt "${MIN_CAPACITY[$MODEL]}" ]; then
            echo "❌ ERROR: $MODEL in $REGION has insufficient quota."
            INSUFFICIENT_QUOTA=true
            break
        fi
    done

    if [ "$INSUFFICIENT_QUOTA" = false ]; then
        VALID_REGION="$REGION"
        break
    fi

done

if [ -z "$VALID_REGION" ]; then
    echo "❌ No region with sufficient quota found. Blocking deployment."
    echo "QUOTA_FAILED=true" >> "$GITHUB_ENV"
    exit 0
else
    echo "✅ Final Region: $VALID_REGION"
    echo "VALID_REGION=$VALID_REGION" >> "$GITHUB_ENV"
    exit 0
fi