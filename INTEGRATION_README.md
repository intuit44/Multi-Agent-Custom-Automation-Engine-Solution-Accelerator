# Integración BoatRental + Multi-Agent Engine

Esta carpeta contiene la integración del proyecto BoatRental con el Multi-Agent Custom Automation Engine.

## 🔧 Configuración

1. Copiar `.env.example` a `.env` y configurar las variables
2. Instalar dependencias: `npm install`
3. Ejecutar tests de conexión: `powershell .\test-foundry-connection.ps1`

## 📁 Estructura

```
.codegpt/
  └── agents.yaml        # Configuración de agentes Codex + AI Foundry
src/agents/
  └── agent975/          # Agente analizador de TSX
      ├── handler.js     # Lógica del agente
      └── package.json   # Dependencias
docs/
  └── ASYNC_GUIDE.md     # Guía de integración asíncrona
```

## 🚀 Uso

### Con Codex
```bash
@codex analyze-component --file HomeScreen.tsx --async
```

### Directo con AI Foundry
```bash
curl -X POST https://boatrentalfoundry-dev.services.ai.azure.com/api/projects/booking-agents/agents/Agent975/invoke \
  -H "Authorization: Bearer $(az account get-access-token --resource https://ai.azure.com --query accessToken -o tsv)" \
  -H "Content-Type: application/json" \
  -d '{"input": {"code": "const Component = () => <div>Hello</div>"}}'
```

## 📊 Agentes Disponibles

- **Agent975**: Análisis de componentes TSX
- **ReadTsxAgent**: Análisis profundo con GPT-4o
- **RefactorAgent**: Refactorización de código
- **PerformanceOptimizer**: Optimización de rendimiento
- **TestingExpert**: Generación de tests

## 🔗 Enlaces

- [Documentación principal](../README.md)
- [Guía asíncrona](docs/ASYNC_GUIDE.md)
- [Configuración de agentes](.codegpt/agents.yaml)
