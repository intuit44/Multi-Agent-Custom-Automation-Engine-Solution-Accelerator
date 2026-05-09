#!/bin/bash

# Diagnosticar por qué el contenedor no arranca
# Uso: ./scripts/diagnose-container.sh <BACKEND_RG> <BACKEND_CONTAINER_APP>

set -e

RG="${1:-boat-rental-app-group-eus2-clean}"
APP="${2:-ca-macae-wsxymdijs3u2}"

echo "🔍 Diagnóstico del Container App"
echo "================================="
echo "Resource Group: $RG"
echo "Container App: $APP"
echo ""

# 1. Ver estado de la aplicación
echo "📊 Estado actual:"
az containerapp show -n "$APP" -g "$RG" \
  --query '{state: properties.provisioningState, active: properties.workloadProfileName}' -o json

echo ""

# 2. Ver últimas revisiones
echo "📋 Últimas revisiones:"
az containerapp revision list -n "$APP" -g "$RG" \
  --query "sort_by(@, &properties.createdTime)[-5:].{name:name, runningState:properties.runningState, healthState:properties.healthState, createdTime:properties.createdTime, replicas:properties.replicas}" \
  -o table

echo ""

# 3. Ver logs de la última revisión
echo "📝 Logs de la última revisión:"
LATEST_REV=$(az containerapp revision list -n "$APP" -g "$RG" \
  --query "sort_by(@, &properties.createdTime)[-1].name" -o tsv)

echo "Revisión: $LATEST_REV"
echo ""

echo "🔴 STDERR/STDOUT:"
az containerapp logs show -n "$APP" -g "$RG" --revision "$LATEST_REV" --tail 100 2>&1 || echo "No logs available"

echo ""

# 4. Ver configuración de la imagen
echo "🐳 Configuración de imagen:"
az containerapp show -n "$APP" -g "$RG" \
  --query "properties.template.containers[0].{image:image, resources:resources}" -o json

echo ""

# 5. Ver variables de entorno (sin valores sensibles)
echo "🔐 Variables de entorno (nombre solo):"
az containerapp show -n "$APP" -g "$RG" \
  --query "properties.template.containers[0].env[].{name:name}" -o table

echo ""
echo "💡 Diagnóstico completado."
echo ""
echo "Posibles causas de ActivationFailed:"
echo "  1. Error en el código/aplicación backend"
echo "  2. Dependencias de bases de datos/servicios no disponibles"
echo "  3. Variables de entorno faltantes o incorrectas"
echo "  4. Puerto incorrecto (healthz en puerto 8000?)"
echo "  5. Imagen Docker corrupta o no existe en ACR"
echo ""
echo "Próximos pasos:"
echo "  1. Revisar los logs arriba"
echo "  2. Verificar src/backend/Dockerfile"
echo "  3. Verificar src/backend/app.py (puerto, healthz endpoint)"
echo "  4. Hacer push manual a ACR y verificar imagen"
