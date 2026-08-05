#!/bin/bash
# fix_webapp_obo.sh - Configurar Web App EasyAuth para OBO
set -e

WEBAPP="app-pslc25991vme66zmins"
RG="arg-macaev4-8d1cceac"
AUTH_APP_ID="ee7ae9f0-67c2-4370-9a9f-1d497a506140"
SECRET=""  # REEMPLAZA CON TU SECRET COMPLETO

echo "🔧 Configurando EasyAuth del Web App para OBO"
echo ""

# 1. Verificar configuración actual
echo "1️⃣ Verificando configuración actual..."
APP_ID=$(az webapp show -n "$WEBAPP" -g "$RG" --query id -o tsv)
echo "✅ Web App ID: $APP_ID"
echo ""

# 2. Mostrar configuración actual de auth
echo "2️⃣ Configuración actual de Azure AD:"
az resource show \
  --ids "$APP_ID/config/authsettingsV2" \
  --query "properties.identityProviders.azureActiveDirectory" \
  -o json
echo ""

# 3. Guardar el secret como app setting
echo "3️⃣ Guardando client secret como app setting..."
az webapp config appsettings set \
  -n "$WEBAPP" \
  -g "$RG" \
  --settings MICROSOFT_PROVIDER_AUTHENTICATION_SECRET="$SECRET" \
  -o none
echo "✅ Secret guardado en app settings"
echo ""

# 4. Configurar el secret en authsettingsV2
echo "4️⃣ Configurando referencia al secret en EasyAuth..."
az resource update \
  --ids "$APP_ID/config/authsettingsV2" \
  --set properties.identityProviders.azureActiveDirectory.registration.clientSecretSettingName="MICROSOFT_PROVIDER_AUTHENTICATION_SECRET" \
  -o none
echo "✅ Secret referenciado en EasyAuth"
echo ""

# 5. Verificar loginParameters (ya debería estar correcto)
echo "5️⃣ Verificando loginParameters..."
LOGIN_PARAMS=$(az resource show \
  --ids "$APP_ID/config/authsettingsV2" \
  --query "properties.identityProviders.azureActiveDirectory.login.loginParameters[0]" \
  -o tsv)
echo "Current loginParameters: $LOGIN_PARAMS"

if [[ "$LOGIN_PARAMS" != *"api://$AUTH_APP_ID/user_impersonation"* ]]; then
    echo "⚠️  loginParameters no incluye la audiencia correcta, actualizando..."
    az resource update \
      --ids "$APP_ID/config/authsettingsV2" \
      --set properties.identityProviders.azureActiveDirectory.login.loginParameters="[\"scope=openid profile email offline_access api://$AUTH_APP_ID/user_impersonation\"]" \
      -o none
    echo "✅ loginParameters actualizado"
else
    echo "✅ loginParameters ya está correcto"
fi
echo ""

# 6. Verificar allowedAudiences
echo "6️⃣ Configurando allowedAudiences..."
az resource update \
  --ids "$APP_ID/config/authsettingsV2" \
  --set properties.identityProviders.azureActiveDirectory.validation.allowedAudiences="[\"api://$AUTH_APP_ID\"]" \
  -o none
echo "✅ allowedAudiences configurado"
echo ""

# 7. Reiniciar Web App
echo "7️⃣ Reiniciando Web App para aplicar cambios..."
az webapp restart -n "$WEBAPP" -g "$RG" -o none
echo "✅ Web App reiniciado"
echo ""

# 8. Mostrar configuración final
echo "8️⃣ Configuración final de Azure AD:"
az resource show \
  --ids "$APP_ID/config/authsettingsV2" \
  --query "{clientId: properties.identityProviders.azureActiveDirectory.registration.clientId, clientSecretSetting: properties.identityProviders.azureActiveDirectory.registration.clientSecretSettingName, loginParameters: properties.identityProviders.azureActiveDirectory.login.loginParameters, allowedAudiences: properties.identityProviders.azureActiveDirectory.validation.allowedAudiences}" \
  -o json
echo ""

echo "✅ Configuración completa!"
echo ""
echo "📋 Próximos pasos:"
echo "   1. Espera 30 segundos para que el Web App reinicie"
echo "   2. Ve a: https://app-pslc25991vme66zmins.azurewebsites.net/"
echo "   3. Haz logout si ya estabas logueado"
echo "   4. Haz login nuevamente"
echo "   5. Debería funcionar sin error 401"
