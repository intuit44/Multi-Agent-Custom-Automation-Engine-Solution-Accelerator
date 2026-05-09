# Guía de Inicio Rápido - Multi-Agent Custom Automation Engine

## 📋 Resumen del Análisis

### Flujo de Arquitectura
```
Usuario → UI (Frontend React/Vite:3001)
Frontend → Backend (FastAPI/uvicorn:8000) → /api/input_task
Backend → Planner Agent (Semantic Kernel)
Planner → Agentes especializados (HR, Marketing, Product, etc.)
Resultado → Backend → Frontend → Usuario
```

### ¿Necesito Docker/Dev Container?
**NO para desarrollo local.** El proyecto puede ejecutarse completamente sin Docker:
- **Dev Container**: Opcional, útil para entorno consistente pero **no requerido**
- **Docker**: Solo necesario para **deployment en Azure Container Apps**
- **Local**: Puedes ejecutar backend (uvicorn) y frontend (Vite) directamente

### ¿CodeGPT es necesario?
**NO es obligatorio.** Los archivos `.codegpt/agents.yaml` son para integración con CodeGPT IDE extension (asistencia de código), pero el proyecto funciona sin él. Es una herramienta de desarrollo, no de ejecución.

---

## 🚀 Pasos para Iniciar el Proyecto

### Prerequisitos
- Python 3.11+
- Node.js 18+ (incluye npm)
- Acceso a Azure (ya tienes configuración en copiloto-function\.env)

### Paso 1: Configurar Entorno Virtual Python (Backend)

```powershell
# Navegar al directorio del backend
cd "c:\ProyectosSimbolicos\boat-rental-app\Multi-Agent-Custom-Automation-Engine-Solution-Accelerator\src\backend"

# Crear entorno virtual
python -m venv .venv

# Activar entorno virtual (Windows)
.\.venv\Scripts\Activate.ps1

# Instalar dependencias del backend
pip install -r requirements.txt
```

### Paso 2: Iniciar el Backend (FastAPI + uvicorn)

```powershell
# Asegúrate de estar en src/backend con el venv activado
cd "c:\ProyectosSimbolicos\boat-rental-app\Multi-Agent-Custom-Automation-Engine-Solution-Accelerator\src\backend"

# Iniciar servidor uvicorn
uvicorn app_kernel:app --host 0.0.0.0 --port 8000 --reload
```

El backend estará disponible en: `http://localhost:8000`
- API Docs (Swagger): `http://localhost:8000/docs`
- Health Check: `http://localhost:8000/health`

### Paso 3: Configurar Frontend (React + Vite)

**En una nueva terminal:**

```powershell
# Navegar al directorio del frontend
cd "c:\ProyectosSimbolicos\boat-rental-app\Multi-Agent-Custom-Automation-Engine-Solution-Accelerator\src\frontend"

# Instalar dependencias (incluye Vite)
npm install

# Iniciar el servidor de desarrollo
npm run dev
```

El frontend estará disponible en: `http://localhost:3001`

---

## 📁 Archivos .env Creados

### Backend (.env) - `src/backend/.env`
Variables configuradas desde `copiloto-function\.env`:
- **CosmosDB**: Para persistencia de memoria de agentes
- **Azure OpenAI**: Para procesamiento de lenguaje natural
- **Azure AI Foundry**: Para orquestación de agentes
- **Application Insights**: Para monitoreo

### Frontend (.env) - `src/frontend/.env`
- `API_URL=http://localhost:8000` - Apunta al backend local
- `ENABLE_AUTH=false` - Desactiva autenticación para desarrollo

---

## 🔧 Configuración de Servicios Azure Necesarios

El proyecto usa estos servicios (ya configurados en tus variables):

| Servicio | Variable | Estado |
|----------|----------|--------|
| **Cosmos DB** | `COSMOSDB_ENDPOINT` | ✅ Configurado |
| **Azure OpenAI** | `AZURE_OPENAI_ENDPOINT` | ✅ Configurado |
| **AI Foundry** | `AZURE_AI_AGENT_ENDPOINT` | ✅ Configurado |
| **App Insights** | `APPLICATIONINSIGHTS_CONNECTION_STRING` | ✅ Configurado |

### Verificar Base de Datos Cosmos DB
El proyecto necesita una base de datos `macae` con contenedor `memory`. Si no existe:

```powershell
# Usando Azure CLI (ya debes estar logueado)
az cosmosdb sql database create --account-name copiloto-cosmos --resource-group boat-rental-app-group --name macae

az cosmosdb sql container create --account-name copiloto-cosmos --resource-group boat-rental-app-group --database-name macae --name memory --partition-key-path /session_id
```

---

## 🧪 Probar el Sistema

### 1. Verificar Backend
```powershell
# Probar endpoint de health
Invoke-RestMethod -Uri "http://localhost:8000/health" -Method GET

# Probar listar herramientas de agentes
Invoke-RestMethod -Uri "http://localhost:8000/api/agent-tools" -Method GET
```

### 2. Desde el Frontend
1. Abrir `http://localhost:3001`
2. Crear una tarea como: "Schedule a new employee orientation for John Doe starting next Monday"
3. El Planner Agent creará un plan y lo asignará al HR_Agent

---

## ⚠️ Notas Importantes

### Error del Dev Container
```
Error: Cannot find module 'devContainersSpecCLI.js'
```
**Solución**: Ignora este error. No necesitas Dev Container para desarrollo local.

### Docker Desktop no configurado
El mensaje `Docker Desktop esta corriendo mas no configurado` es normal si no usas Docker. Para desarrollo local, **no lo necesitas**.

### Vite no instalado
Vite se instala automáticamente con `npm install` en el frontend (está en `devDependencies` del package.json).

---

## 📊 Arquitectura de Agentes

```
┌─────────────────────────────────────────────────────────────┐
│                    GROUP_CHAT_MANAGER                        │
│              (Orquestador principal de agentes)              │
└─────────────────────┬───────────────────────────────────────┘
                      │
        ┌─────────────┼─────────────┐
        │             │             │
        ▼             ▼             ▼
┌───────────┐ ┌───────────┐ ┌───────────┐
│  PLANNER  │ │   HUMAN   │ │  GENERIC  │
│   AGENT   │ │   AGENT   │ │   AGENT   │
└───────────┘ └───────────┘ └───────────┘
        │
        ├─────────────────────────────────────┐
        │                                     │
        ▼                                     ▼
┌───────────────────────────────┐   ┌───────────────────────────────┐
│  AGENTES ESPECIALIZADOS       │   │  HERRAMIENTAS (kernel_tools)  │
│  ─────────────────────────    │   │  ─────────────────────────    │
│  • HR_Agent                   │   │  • HrTools                    │
│  • Marketing_Agent            │   │  • MarketingTools             │
│  • Procurement_Agent          │   │  • ProcurementTools           │
│  • Product_Agent              │   │  • ProductTools               │
│  • Tech_Support_Agent         │   │  • TechSupportTools           │
└───────────────────────────────┘   └───────────────────────────────┘
```

---

## 🔄 Comandos Rápidos

```powershell
# === INICIAR TODO ===

# Terminal 1: Backend
cd "c:\ProyectosSimbolicos\boat-rental-app\Multi-Agent-Custom-Automation-Engine-Solution-Accelerator\src\backend"
.\.venv\Scripts\Activate.ps1
uvicorn app_kernel:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2: Frontend
cd "c:\ProyectosSimbolicos\boat-rental-app\Multi-Agent-Custom-Automation-Engine-Solution-Accelerator\src\frontend"
npm run dev
```

---

## 📚 Referencias
- [Semantic Kernel Documentation](https://learn.microsoft.com/en-us/semantic-kernel/)
- [Azure AI Foundry Documentation](https://learn.microsoft.com/en-us/azure/ai-foundry/)
- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Vite Documentation](https://vitejs.dev/)
