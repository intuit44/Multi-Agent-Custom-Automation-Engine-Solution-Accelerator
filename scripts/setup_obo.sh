#!/bin/bash
# Script para configurar OBO en macae-v4-auth
set -e

AUTH_APP_ID="ee7ae9f0-67c2-4370-9a9f-1d497a506140"
WORKIQ_APP_ID="ea9ffc3e-8a23-4a7d-836d-234d7c7565c1"  # Reemplazar con el ID real de WorkIQ

echo "🔧 Configurando OBO para macae-v4-auth"
echo ""

# 1. Crear client secret
echo "1️⃣ Creando client secret..."
SECRET=$(az ad app credential reset --id "$AUTH_APP_ID" --append --query password -o tsv)
echo "✅ Secret creado (guárdalo en lugar seguro):"
echo "$SECRET"
echo ""

# 2. Obtener el scope ID de WorkIQ
echo "2️⃣ Buscando scope McpServers.CopilotMCP.All en WorkIQ..."
SCOPE_ID=$(az ad sp show --id "$WORKIQ_APP_ID" \
  --query "oauth2PermissionScopes[?value=='McpServers.CopilotMCP.All'].id" -o tsv)

if [ -z "$SCOPE_ID" ]; then
    echo "❌ No se encontró el scope McpServers.CopilotMCP.All"
    exit 1
fi
echo "✅ Scope ID: $SCOPE_ID"
echo ""

# 3. Agregar permiso delegado WorkIQ
echo "3️⃣ Agregando permiso delegado para WorkIQ..."
az ad app permission add \
  --id "$AUTH_APP_ID" \
  --api "$WORKIQ_APP_ID" \
  --api-permissions "${SCOPE_ID}=Scope" 2>/dev/null || echo "⚠️  Permiso ya existe"
echo "✅ Permiso agregado"
echo ""

# 4. Agregar permiso para Cognitive Services (opcional, si es necesario)
echo "4️⃣ Verificando permiso para Cognitive Services..."
COGSERV_APP_ID="https://cognitiveservices.azure.com"
# Nota: Este permiso puede no ser necesario si el OBO es solo para WorkIQ
echo "ℹ️  Permiso de Cognitive Services: verificar manualmente si es necesario"
echo ""

# 5. Admin consent
echo "5️⃣ Otorgando admin consent..."
az ad app permission admin-consent --id "$AUTH_APP_ID"
echo "✅ Admin consent otorgado"
echo ""

# 6. Mostrar comando para Container App
echo "📋 Comandos para configurar Container App:"
echo ""
echo "RG=\"arg-macaev4-8d1cceac\""
echo "CA=\"ca-pslc25991vme66zmins\""
echo ""
echo "# Guardar secret"
echo "az containerapp secret set -n \"\$CA\" -g \"\$RG\" \\"
echo "  --secrets obo-client-secret=\"$SECRET\""
echo ""
echo "# Configurar env vars"
echo "az containerapp update -n \"\$CA\" -g \"\$RG\" --set-env-vars \\"
echo "  ENABLE_OBO=true \\"
echo "  OBO_CLIENT_ID=$AUTH_APP_ID \\"
echo "  OBO_CLIENT_SECRET=secretref:obo-client-secret"
echo ""
echo "✅ Configuración completa"
