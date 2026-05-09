# Guía: Desplegar Backend en Azure Container Apps

## Estado actual
- ✅ Functions (Timer + Consumer) en Azure
- ✅ Service Bus configurado
- ❌ **Backend NO está en Azure** - Timer llama a `localhost:8000` que no existe
- ❌ Loop estratégico INCOMPLETO

## Qué faltaba
Tu `azure.yaml` era un esqueleto sin:
- Sección `infra:` (para provisionar Container App con `azd provision`)
- Sección `services:` (para buildear imagen Docker y desplegar con `azd deploy`)

Se ha actualizado `azure.yaml` para orquestar todo.

---

## 🚀 Pasos para Desplegar

### Paso 1: Inicializar ambiente azd (si no lo has hecho)

```bash
cd /workspaces/Multi-Agent-Custom-Automation-Engine-Solution-Accelerator
azd init -e boat-rental-app-group
```

**Qué hace:**
- Crea `.azure/boat-rental-app-group` con la configuración de tu entorno
- Vincula la suscripción Azure y resource group

### Paso 2: Provisionar infraestructura (Container App + Dependencies)

```bash
azd provision
```

**Qué hace (3-4 minutos):**
1. Lee `infra/main.bicep`
2. Crea:
   - Container App Environment
   - Container App (para backend)
   - Virtual Network (si WAF=true)
   - Log Analytics + Application Insights
   - Resource Group tags

**Salida esperada:**
```
Provisioning resources...
...
Deployment complete:
  BACKEND_URL=https://ca-macae-xxx.eastus.azurecontainerapps.io
  COSMOSDB_ENDPOINT=https://cosmos-macae-xxx.documents.azure.com/
  ...
```

**Captura la URL del backend** (verás algo como `https://ca-macae-xxxx.eastus.azurecontainerapps.io`)

### Paso 3: Buildear y desplegar backend (imagen Docker)

```bash
azd deploy
```

**Qué hace (2-3 minutos):**
1. Lee `src/backend/Dockerfile`
2. Buildea imagen: `macaebackend:${AZURE_ENV_IMAGE_TAG}`
3. Pushea a Azure Container Registry
4. Actualiza Container App con nueva imagen
5. Container App inicia con réplicas (scale=1)

**Salida esperada:**
```
Creating container image...
Pushing image to registry...
Deploying to container app...
Deployment complete. Backend running.
```

### Paso 4: Obtener URL pública del backend

```bash
BACKEND_FQDN=$(az containerapp show \
  -g boat-rental-app-group \
  -n ca-macae-xxxx \
  --query "properties.configuration.ingress.fqdn" -o tsv)

echo "Backend URL: https://$BACKEND_FQDN"
```

Verifica que responde:
```bash
curl -s "https://$BACKEND_FQDN/api/observability/snapshot" | jq .
```

Debe devolver `200 OK` con health snapshot.

### Paso 5: Actualizar Timer Function App con URL real

```bash
# Reemplaza XXX con tu FQDN real
BACKEND_FQDN="ca-macae-xxxx.eastus.azurecontainerapps.io"

az functionapp config appsettings set \
  -g boat-rental-app-group \
  -n fibroskin-strategic-timer \
  --settings ACCELERATOR_API_BASE_URL="https://$BACKEND_FQDN"
```

**Verifica la actualización:**
```bash
az functionapp config appsettings list \
  -g boat-rental-app-group \
  -n fibroskin-strategic-timer \
  --query "[?name=='ACCELERATOR_API_BASE_URL'].value" -o tsv
```

### Paso 6: Validar E2E (Loop completo)

#### 6.1 Trigger manual del Timer desde Azure

```bash
# Acceder a la UI del Portal Azure > Function Apps > fibroskin-strategic-timer > Functions > strategic_autonomy_timer
# O ejecutar via curl con función key:

FUNC_KEY=$(az functionapp keys list -g boat-rental-app-group -n fibroskin-strategic-timer --query "functionKeys.default" -o tsv)

curl -X POST "https://fibroskin-strategic-timer.azurewebsites.net/admin/functions/strategic_autonomy_timer" \
  -H "x-functions-key: $FUNC_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"test"}'
```

#### 6.2 Verificar que Timer llamó a Backend

```bash
# Ver logs del backend (Container App)
az containerapp logs show \
  -g boat-rental-app-group \
  -n ca-macae-xxxx \
  --follow
```

Busca líneas como:
```
POST /api/strategic/analyze
200 OK
health_score=...
publication_status=...
```

#### 6.3 Verificar que mensaje llegó a Service Bus (si health_score < 50)

```bash
az servicebus topic show \
  -g boat-rental-app-group \
  --namespace-name fibroskin-servicebus \
  --name strategic-decisions \
  --query "countDetails.activeMessageCount"
```

#### 6.4 Verificar que Consumer procesó mensaje

```bash
az webapp log tail -g boat-rental-app-group -n fibroskin-strategic-consumer
```

Busca:
```
Strategic decision received
Message processed successfully
```

---

## ✅ Checklist: Loop Completo Operativo

- [ ] `azd init -e boat-rental-app-group` completó sin errores
- [ ] `azd provision` creó Container App (verifica FQDN)
- [ ] `azd deploy` buildó y desplegó imagen
- [ ] `curl https://<backend-fqdn>/api/observability/snapshot` devuelve 200
- [ ] Timer tiene `ACCELERATOR_API_BASE_URL=https://<backend-fqdn>` (verificado)
- [ ] Trigger manual de Timer → Backend recibe POST
- [ ] Backend responde con health snapshot
- [ ] Si health_score < 50: Mensaje en Service Bus
- [ ] Consumer procesa mensaje (verifica logs)

---

## 🔧 Troubleshooting

### Container App no inicia
```bash
# Ver logs de la Container App
az containerapp logs show -g boat-rental-app-group -n ca-macae-xxxx --follow

# Ver health del container
az containerapp show -g boat-rental-app-group -n ca-macae-xxxx \
  --query "properties.runningStatus"
```

**Causas comunes:**
- `AZURE_OPENAI_ENDPOINT` no alcanzable (check red)
- `COSMOSDB_ENDPOINT` no alcanzable (check VNET/NSG)
- Imagen Docker no existe en ACR

### Timer no puede llamar a backend
```bash
# Verificar URL en Timer
az functionapp config appsettings list -g boat-rental-app-group -n fibroskin-strategic-timer

# Ver logs del Timer
az webapp log tail -g boat-rental-app-group -n fibroskin-strategic-timer
```

**Causas comunes:**
- URL todavía es `localhost`
- Backend Container App no responde (ver logs del app)
- Network policies bloqueando (check NSG si WAF=true)

### Backend devuelve error
```bash
# Curl directo al backend
curl -v https://<backend-fqdn>/api/observability/snapshot

# Ver logs del backend
az containerapp logs show -g boat-rental-app-group -n ca-macae-xxxx --follow
```

---

## 📊 Diagrama final: Loop Estratégico Completo

```
AZURE INFRASTRUCTURE
┌─────────────────────────────────────────────────────────────┐
│                                                               │
│  Timer (Function App)                                        │
│  ├─ Schedule: every 15 min (cron)                           │
│  ├─ Trigger: Timer event                                    │
│  └─ POST https://<backend-fqdn>/api/strategic/analyze       │
│         │                                                     │
│         ▼                                                     │
│  Backend (Container App)                                     │
│  ├─ GET Cosmos trends (7 days)                              │
│  ├─ Call agent.analyze_business_context()                   │
│  ├─ Compute health_score                                    │
│  └─ If health_score < 50: Publish to Service Bus            │
│         │                                                     │
│         ▼                                                     │
│  Service Bus (Topic: strategic-decisions)                    │
│  ├─ Message: {"decision":"escalate", "reason":"..."} TTL:1h │
│  └─ Subscription: critical-failure-handler (dead-lettering) │
│         │                                                     │
│         ▼                                                     │
│  Consumer (Function App)                                     │
│  ├─ Trigger: Service Bus message                            │
│  ├─ Process decision (optional: forward to webhook)         │
│  └─ Log processing result                                   │
│                                                               │
│  📊 Observability                                            │
│  ├─ Container App logs: Backend requests/responses          │
│  ├─ Log Analytics: Queries on observability events          │
│  └─ Application Insights: APM metrics                        │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

---

## 📝 Notas importantes

1. **Azure CLI debe estar autenticado**: `az account show`
2. **Imagen Docker debe buildear sin errores**: `docker build src/backend -t test`
3. **Cuotas de GPT**: Asegúrate de tener cuota para `gpt-4o` (140 tokens)
4. **WAF vs Sandbox**: Por defecto `useWafAlignedArchitecture=false` (más barato)
5. **Costos**: Container App escala a cero cuando idle, económico para dev/test

---

## 🎯 Resultado esperado

Después de completar todos los pasos:

✅ Backend corriendo en Azure Container Apps con FQDN público  
✅ Timer Function llamando al backend cada 15 minutos  
✅ Backend publicando decisiones estratégicas a Service Bus  
✅ Consumer procesando mensajes automáticamente  
✅ Loop completamente operativo y escalable en Azure  

🚀 **Tu solución de autonomía estratégica está LISTA PARA PRODUCCIÓN**

