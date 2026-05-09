#!/bin/bash
set -e  # Detener en cualquier error

# Configuración
ORG="macae-labs"
REPO="Multi-Agent-Custom-Automation-Engine-Solution-Accelerator"

echo "🔍 Verificando autenticación..."
if ! gh auth status >/dev/null 2>&1; then
    echo "❌ No autenticado. Ejecuta: gh auth login"
    exit 1
fi

echo "✅ Autenticación OK"
echo ""

# ============================================
# 1. SECRETS A NIVEL ORGANIZACIÓN
# ============================================
echo "🏢 Configurando secrets de organización..."

gh secret set AZURE_TENANT_ID --org "$ORG" --repos "$REPO" --body "978d9cc6-784c-4c98-8d90-a4a6344a65ff"
gh secret set ALERT_LOGIC_APP_URL --org "$ORG" --repos "$REPO" --body "https://prod-02.eastus2.logic.azure.com:443/workflows/27ab8515014b4acca568f018324d1e06/triggers/When_a_HTTP_request_is_received/paths/invoke?api-version=2016-06-01&sp=%2Ftriggers%2FWhen_a_HTTP_request_is_received%2Frun&sv=1.0&sig=flqzo3DVUgW9HEisUjHQaY3Jox3HBhcrQ4glbk3RKiY"

echo ""

# ============================================
# 2. SECRETS A NIVEL REPOSITORIO
# ============================================
echo "📦 Configurando secrets del repositorio..."

gh secret set AZURE_CLIENT_ID --repo "$ORG/$REPO" --body "04dcd80d-30fe-4895-b199-26bb4e291663"
gh secret set ACR_NAME --repo "$ORG/$REPO" --body "boatrentalacr"
gh secret set BACKEND_CONTAINER_APP --repo "$ORG/$REPO" --body "ca-macae-wsxymdijs3u2"
gh secret set FRONTEND_WEBAPP --repo "$ORG/$REPO" --body "app-macae-wsxymdijs3u2"
gh secret set ACR_LOGIN_SERVER --repo "$ORG/$REPO" --body "boatrentalacr.azurecr.io"

echo ""

# ============================================
# 3. VARIABLES (no secrets)
# ============================================
echo "🔧 Configurando variables del repositorio..."

gh variable set AZURE_REGIONS --repo "$ORG/$REPO" --body "eastus2,eastus"
gh variable set AZURE_LOCATION --repo "$ORG/$REPO" --body "eastus2"

echo ""

# ============================================
# 4. CREAR ENTORNOS (VERSIÓN CORREGIDA)
# ============================================
echo "🌍 Creando entornos..."

# Entorno dev (sin protecciones)
echo "  Creando dev..."
gh api -X PUT "repos/$ORG/$REPO/environments/dev" --silent 2>/dev/null || echo "  ⚠️  dev ya existe o error"

# Entorno prod con branch protection (formato correcto)
echo "  Creando prod..."
gh api -X PUT "repos/$ORG/$REPO/environments/prod" \
  --input - <<EOF 2>/dev/null || echo "  ⚠️  prod ya existe o error"
{
  "deployment_branch_policy": {
    "protected_branches": true,
    "custom_branch_policies": false
  }
}
EOF

echo "  ⚠️  Configura manualmente los reviewers para prod en GitHub UI si es necesario"
echo ""

# Pequeña pausa para que los entornos se propaguen
sleep 2

# ============================================
# 5. SECRETS POR ENTORNO
# ============================================
echo "🔐 Configurando secrets por entorno..."

# ENTORNO DEV
if gh api "repos/$ORG/$REPO/environments/dev" >/dev/null 2>&1; then
    echo "  Configurando dev..."
    gh secret set AZURE_SUBSCRIPTION_ID --env dev --repo "$ORG/$REPO" --body "380fa841-83f3-42fe-adc4-582a5ebe139b"
    gh secret set BACKEND_RG --env dev --repo "$ORG/$REPO" --body "boat-rental-app-group-eus2-clean"
    gh secret set FRONTEND_RG --env dev --repo "$ORG/$REPO" --body "boat-rental-app-group-eus2-clean"
    gh secret set ALERT_LOGIC_APP_URL --env dev --repo "$ORG/$REPO" --body "https://prod-02.eastus2.logic.azure.com:443/workflows/27ab8515014b4acca568f018324d1e06/triggers/When_a_HTTP_request_is_received/paths/invoke?api-version=2016-06-01&sp=%2Ftriggers%2FWhen_a_HTTP_request_is_received%2Frun&sv=1.0&sig=flqzo3DVUgW9HEisUjHQaY3Jox3HBhcrQ4glbk3RKiY"
else
    echo "  ⚠️  Entorno dev no disponible, saltando..."
fi

# ENTORNO PROD
if gh api "repos/$ORG/$REPO/environments/prod" >/dev/null 2>&1; then
    echo "  Configurando prod..."
    gh secret set AZURE_SUBSCRIPTION_ID --env prod --repo "$ORG/$REPO" --body "380fa841-83f3-42fe-adc4-582a5ebe139b"
    gh secret set BACKEND_RG --env prod --repo "$ORG/$REPO" --body "boat-rental-app-group-eus2-prod"
    gh secret set FRONTEND_RG --env prod --repo "$ORG/$REPO" --body "boat-rental-app-group-eus2-prod"
    gh secret set ALERT_LOGIC_APP_URL --env prod --repo "$ORG/$REPO" --body "https://prod-02.eastus2.logic.azure.com:443/workflows/27ab8515014b4acca568f018324d1e06/triggers/When_a_HTTP_request_is_received/paths/invoke?api-version=2016-06-01&sp=%2Ftriggers%2FWhen_a_HTTP_request_is_received%2Frun&sv=1.0&sig=flqzo3DVUgW9HEisUjHQaY3Jox3HBhcrQ4glbk3RKiY"
else
    echo "  ⚠️  Entorno prod no disponible, saltando..."
fi

echo ""

# ============================================
# 6. VERIFICACIÓN
# ============================================
echo "✅ Verificando configuración..."
echo ""

echo "📋 Secrets de organización:"
gh secret list --org "$ORG" || echo "  ⚠️  No se pueden listar org secrets"

echo ""
echo "📋 Secrets del repositorio:"
gh secret list --repo "$ORG/$REPO" || echo "  ⚠️  No se pueden listar repo secrets"

echo ""
echo "📋 Variables del repositorio:"
gh variable list --repo "$ORG/$REPO" || echo "  ⚠️  No se pueden listar variables"

echo ""
echo "📋 Entornos:"
gh api "repos/$ORG/$REPO/environments" --jq '.environments[].name' 2>/dev/null || echo "No se pudieron listar entornos"

echo ""
echo "📋 Secrets del entorno dev:"
gh secret list --env dev --repo "$ORG/$REPO" 2>/dev/null || echo "  ⚠️  No hay secrets en dev o entorno no existe"

echo ""
echo "📋 Secrets del entorno prod:"
gh secret list --env prod --repo "$ORG/$REPO" 2>/dev/null || echo "  ⚠️  No hay secrets en prod o entorno no existe"

echo ""
echo "✅ Configuración completada"