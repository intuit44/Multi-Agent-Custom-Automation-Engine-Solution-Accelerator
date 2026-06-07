# OBO Setup - Checklist Final

## Variables de Entorno

### Backend usa DOS identidades separadas:

```bash
# Managed Identity del backend (para plano de gestión)
AZURE_CLIENT_ID=b06528b9-36fc-4a71-a24e-8f673b787486

# App Registration confidencial (para OBO en plano de datos)
OBO_CLIENT_ID=ee7ae9f0-67c2-4370-9a9f-1d497a506140
OBO_CLIENT_SECRET=secretref:obo-client-secret  # referencia al secreto
OBO_TENANT_ID=<tenant-id>  # opcional, cae a AZURE_TENANT_ID

# Flag de activación
ENABLE_OBO=true
```

## Pasos de Configuración

### ✅ 1. EasyAuth - Web App (COMPLETADO)

```bash
WEBAPP="app-pslc25991vme66zmins"
RG="arg-macaev4-8d1cceac"
AUTH_APP_ID="ee7ae9f0-67c2-4370-9a9f-1d497a506140"

APP_ID=$(az webapp show -n "$WEBAPP" -g "$RG" --query id -o tsv)

az resource update \
  --ids "$APP_ID/config/authsettingsV2" \
  --set properties.identityProviders.azureActiveDirectory.login.loginParameters='["scope=openid profile email offline_access api://'"$AUTH_APP_ID"'/user_impersonation"]'
```

**Resultado**: EasyAuth emite tokens con `aud=api://ee7ae9f0-.../`

---

### ⏳ 2. App Registration macae-v4-auth (ee7ae9f0)

#### 2.1. Crear Client Secret

```bash
AUTH_APP_ID="ee7ae9f0-67c2-4370-9a9f-1d497a506140"

# Crear secret con duración de 12 meses
SECRET=$(az ad app credential reset \
  --id "$AUTH_APP_ID" \
  --append \
  --years 1 \
  --query password -o tsv)

echo "⚠️  GUARDA ESTE SECRET (no se mostrará de nuevo):"
echo "$SECRET"
```

#### 2.2. Agregar Permisos Delegados

**Permisos requeridos:**

1. **Azure Cognitive Services** - `user_impersonation`
   - Para que OBO pueda canjear hacia `/responses`
   
2. **WorkIQ (ea9ffc3e-...)** - `McpServers.CopilotMCP.All`
   - Para que Foundry ARA pueda ejecutar herramientas MCP

```bash
# 1. Cognitive Services (user_impersonation)
COG_SERVICES_APP_ID="00000000-0000-0000-0000-000000000000"  # reemplazar con ID real
COG_SCOPE_ID=$(az ad sp show --id "$COG_SERVICES_APP_ID" \
  --query "oauth2PermissionScopes[?value=='user_impersonation'].id" -o tsv)

az ad app permission add \
  --id "$AUTH_APP_ID" \
  --api "$COG_SERVICES_APP_ID" \
  --api-permissions "${COG_SCOPE_ID}=Scope"

# 2. WorkIQ (McpServers.CopilotMCP.All)
WORKIQ_APP_ID="ea9ffc3e-..."  # reemplazar con ID real de WorkIQ
WORKIQ_SCOPE_ID=$(az ad sp show --id "$WORKIQ_APP_ID" \
  --query "oauth2PermissionScopes[?value=='McpServers.CopilotMCP.All'].id" -o tsv)

az ad app permission add \
  --id "$AUTH_APP_ID" \
  --api "$WORKIQ_APP_ID" \
  --api-permissions "${WORKIQ_SCOPE_ID}=Scope"
```

#### 2.3. Admin Consent

```bash
# Conceder admin consent para TODOS los permisos delegados
az ad app permission admin-consent --id "$AUTH_APP_ID"
```

**Verificar en Portal:**
- Azure Portal → App registrations → macae-v4-auth
- API permissions → Status = "Granted for [tenant]"

---

### ⏳ 3. Backend - Build & Deploy

```bash
cd /workspaces/Multi-Agent-Custom-Automation-Engine-Solution-Accelerator

# Verificar que el código compila
cd src/backend
python -m py_compile common/config/app_config.py

# Build imagen Docker
cd ../..
docker build -t boatrentalacr.azurecr.io/macaebackend:v5.3.3-obo -f src/backend/Dockerfile .

# Push a ACR
az acr login --name boatrentalacr
docker push boatrentalacr.azurecr.io/macaebackend:v5.3.3-obo
```

---

### ⏳ 4. Container App - Configurar Secretos y Env Vars

```bash
RG="arg-macaev4-8d1cceac"
CA="ca-pslc25991vme66zmins"
AUTH_APP_ID="ee7ae9f0-67c2-4370-9a9f-1d497a506140"

# 1. Guardar el secret como secreto del Container App (usar el secret del paso 2.1)
az containerapp secret set \
  -n "$CA" \
  -g "$RG" \
  --secrets obo-client-secret="<SECRET-DEL-PASO-2.1>"

# 2. Actualizar imagen y variables de entorno
az containerapp update \
  -n "$CA" \
  -g "$RG" \
  --image boatrentalacr.azurecr.io/macaebackend:v5.3.3-obo \
  --set-env-vars \
    ENABLE_OBO=true \
    OBO_CLIENT_ID="$AUTH_APP_ID" \
    OBO_CLIENT_SECRET=secretref:obo-client-secret

# 3. Restart (si es necesario)
az containerapp revision restart \
  -n "$CA" \
  -g "$RG" \
  --revision $(az containerapp revision list -n "$CA" -g "$RG" --query "[0].name" -o tsv)
```

**Nota**: `secretref:obo-client-secret` es una REFERENCIA segura, no el secret en texto plano.

---

### ⏳ 5. Verificar Configuración

```bash
# Ver las env vars actuales
az containerapp show -n "$CA" -g "$RG" \
  --query "properties.template.containers[0].env[?name=='ENABLE_OBO' || name=='OBO_CLIENT_ID']"

# Debería mostrar:
# [
#   {"name": "ENABLE_OBO", "value": "true"},
#   {"name": "OBO_CLIENT_ID", "value": "ee7ae9f0-..."},
#   {"name": "OBO_CLIENT_SECRET", "secretRef": "obo-client-secret"}
# ]
```

---

### ⏳ 6. Testing

#### 6.1. Logout/Login en Frontend

1. Abrir `https://app-pslc25991vme66zmins.azurewebsites.net`
2. Logout (borrar sesión)
3. Login de nuevo
4. **Verificar token**: El nuevo token debe tener `aud=api://ee7ae9f0-...`

Para verificar el token:
```bash
# Extraer token del header X-MS-TOKEN-AAD-ACCESS-TOKEN
# Decodificar en https://jwt.ms
# Verificar: "aud": "api://ee7ae9f0-67c2-4370-9a9f-1d497a506140"
```

#### 6.2. Probar WorkIQ

1. Crear nueva conversación
2. Invocar agente que use WorkIQ MCP server
3. **Esperado**: Herramienta WorkIQ se ejecuta sin error 401/403

#### 6.3. Logs en Tiempo Real

```bash
# Stream logs del Container App
az containerapp logs show \
  -n "$CA" \
  -g "$RG" \
  --follow \
  --tail 50

# Buscar líneas clave:
# ✅ "OBO client_id usado: ee7ae9f0"
# ✅ "credencial: OnBehalfOfCredential"
# ❌ "ENABLE_OBO set but OBO credential unavailable" → revisar secret
# ❌ "invalid_grant" → revisar audience del token EasyAuth
# ❌ "consent required" → revisar admin consent
```

---

## Diagrama de Flujo OBO

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. User → EasyAuth                                             │
│    Scope: api://ee7ae9f0.../user_impersonation                │
│    Token: aud=api://ee7ae9f0-... (assertion válida para OBO)  │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│ 2. Backend (OnBehalfOfCredential)                              │
│    tenant_id: <tenant>                                         │
│    client_id: ee7ae9f0 (app registration, NO la MI)           │
│    client_secret: <del secreto>                                │
│    user_assertion: token de EasyAuth                           │
│    ────────────────────────────────────────────────────────    │
│    Exchange → token con aud=cognitiveservices.azure.com        │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│ 3. Azure AI Foundry /responses                                 │
│    Acepta token (aud=cognitiveservices)                        │
│    Foundry ARA realiza OTRO OBO:                               │
│      - assertion: token ORIGINAL (aud=ee7ae9f0)                │
│      - scope: api://ea9ffc3e-.../McpServers.CopilotMCP.All     │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│ 4. WorkIQ MCP Server                                           │
│    Valida: app ee7ae9f0 tiene permiso delegado                │
│    Ejecuta herramienta con identidad del usuario               │
└─────────────────────────────────────────────────────────────────┘
```

---

## Troubleshooting

### Error: "invalid_grant: AADSTS50013"

**Causa**: Token EasyAuth tiene audiencia incorrecta

**Fix**:
```bash
# Verificar EasyAuth loginParameters
az resource show \
  --ids "$APP_ID/config/authsettingsV2" \
  --query "properties.identityProviders.azureActiveDirectory.login.loginParameters"

# Debe incluir: "api://ee7ae9f0-.../user_impersonation"
```

---

### Error: "AADSTS65001: consent required"

**Causa**: Falta admin consent

**Fix**:
```bash
az ad app permission admin-consent --id ee7ae9f0-67c2-4370-9a9f-1d497a506140
```

---

### Warning: "OBO credential unavailable; falling back"

**Causa**: `ENABLE_OBO=true` pero no hay `OBO_CLIENT_SECRET`

**Fix**:
```bash
# Verificar secreto existe
az containerapp secret list -n "$CA" -g "$RG"

# Recrear si falta
az containerapp secret set -n "$CA" -g "$RG" --secrets obo-client-secret="<secret>"

# Actualizar env var
az containerapp update -n "$CA" -g "$RG" \
  --set-env-vars OBO_CLIENT_SECRET=secretref:obo-client-secret
```

---

### Error: "TokenCredential: OnBehalfOfCredential.get_token failed"

**Causa**: Cliente OBO mal configurado o secret inválido

**Fix**:
1. Verificar que `OBO_CLIENT_ID=ee7ae9f0-...` (NO b06528b9 que es la MI)
2. Regenerar secret si expiró
3. Verificar `OBO_CLIENT_SECRET` apunta al secreto correcto

---

## Rollback

Si algo falla, deshabilitar OBO sin cambiar nada más:

```bash
az containerapp update \
  -n "$CA" \
  -g "$RG" \
  --set-env-vars ENABLE_OBO=false

# O remover completamente
az containerapp update \
  -n "$CA" \
  -g "$RG" \
  --remove-env-vars ENABLE_OBO
```

El backend cae automáticamente a `StaticTokenCredential` (passthrough).

---

## Estado Actual

- [x] EasyAuth configurado (scope correcto)
- [x] Código backend actualizado (variables OBO_*)
- [ ] Secret creado en app ee7ae9f0
- [ ] Permisos delegados agregados
- [ ] Admin consent otorgado
- [ ] Backend deployado con imagen nueva
- [ ] Container App configurado con env vars OBO
- [ ] Testing con WorkIQ
- [ ] Logs validados

---

## Comandos Rápidos de Verificación

```bash
# Ver estado del Container App
az containerapp show -n ca-pslc25991vme66zmins -g arg-macaev4-8d1cceac \
  --query "{image: properties.template.containers[0].image, obo_enabled: properties.template.containers[0].env[?name=='ENABLE_OBO'].value}"

# Ver logs recientes
az containerapp logs show -n ca-pslc25991vme66zmins -g arg-macaev4-8d1cceac --tail 20

# Ver permisos de la app
az ad app permission list --id ee7ae9f0-67c2-4370-9a9f-1d497a506140

# Ver EasyAuth config
az webapp auth show -n app-pslc25991vme66zmins -g arg-macaev4-8d1cceac \
  --query "identityProviders.azureActiveDirectory.login.loginParameters"
```
