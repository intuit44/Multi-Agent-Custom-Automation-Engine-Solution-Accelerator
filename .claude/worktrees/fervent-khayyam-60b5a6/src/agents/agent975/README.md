# Agent975 - Azure AI Foundry TSX Analyzer

## 📋 Descripción

Agent975 es un agente especializado en el análisis de componentes TSX/React Native, integrado con Azure AI Foundry. Proporciona análisis de complejidad, detección de problemas de rendimiento y sugerencias de mejora para código React Native.

## 🔧 Instalación

```bash
# Desde el directorio del agente
npm install

# O desde la raíz del proyecto
cd src/agents/agent975 && npm install
```

## 🚀 Uso

### Ejecutar análisis estándar
```bash
npm start
```

### Probar conexión con Azure AI Foundry
```bash
npm run test
```

### Validar configuración
```bash
npm run validate
```

### Uso programático
```javascript
import Agent975 from './run-agent975.mjs';

const agent = new Agent975();
await agent.initialize();

const code = `
const MyComponent = ({ data }) => {
  return <FlatList data={data} renderItem={renderItem} />;
};
`;

const result = await agent.analyzeCode(code, {
  complexity: true,
  performance: true,
  suggestions: true
});

console.log(result);
```

## 🔑 Configuración Requerida

### Variables de Entorno (`.env` en la raíz)
```env
AZURE_AI_FOUNDRY_ENDPOINT=https://boatRentalFoundry-dev.services.ai.azure.com
AZURE_AI_FOUNDRY_PROJECT=booking-agents
AGENT975_ID=Agent975
```

### Contexto del Agente (`.codegpt/agents.context.json`)
```json
{
  "version": "1.0.0",
  "agentId": "Agent975",
  "endpoint": "${AZURE_AI_FOUNDRY_ENDPOINT}",
  "project": "${AZURE_AI_FOUNDRY_PROJECT}"
}
```

## 📊 Capacidades

- **Análisis de TSX**: Examina componentes React Native/React
- **Detección de Complejidad**: Identifica código complejo que necesita refactorización
- **Optimización de Rendimiento**: Sugiere mejoras de rendimiento
- **Validación de Patrones**: Verifica buenas prácticas de React Native

## 🔍 Formato de Respuesta

```json
{
  "status": "success",
  "timestamp": "2025-07-31T12:00:00Z",
  "analysis": {
    "lines": 15,
    "size": 345,
    "feedback": [
      "✅ Código recibido correctamente",
      "📏 Líneas de código: 15",
      "⚠️ Considerar memoización para renderItem"
    ]
  },
  "metrics": {
    "complexity": 8,
    "performance_score": 7.5
  },
  "suggestions": [
    "Usar React.memo para componentes puros",
    "Implementar keyExtractor personalizado"
  ]
}
```

## 🐛 Solución de Problemas

### Error: "No se encontró el archivo de contexto"
- Verificar que `.codegpt/agents.context.json` existe
- Ejecutar desde la ubicación correcta

### Error: "401 Unauthorized"
- Verificar credenciales de Azure
- Ejecutar `az login` si es necesario
- Confirmar variables de entorno

### Error: "Agent not found"
- Verificar que el agente existe en Azure AI Foundry
- Confirmar el ID del agente y nombre del proyecto

## 🧪 Testing

```bash
# Ejecutar prueba básica
npm run test

# Analizar un archivo específico
node run-agent975.mjs --file ./sample.tsx

# Modo debug
DEBUG=agent975:* npm start
```

## 📈 Métricas y Monitoreo

El agente puede enviar métricas a Application Insights si está configurado:

```env
APPINSIGHTS_INSTRUMENTATIONKEY=your-key-here
```

Las métricas incluyen:
- Tiempo de análisis
- Tamaño de código procesado
- Errores y excepciones
- Uso del agente

## 🔗 Integración con Otros Agentes

Agent975 se integra con:
- **Architect_BoatRental**: Para análisis arquitectural
- **Mobile_App_Agent**: Para optimizaciones específicas de móvil
- **RefactorAgent**: Para refactorizaciones complejas

## 📝 Licencia

Este agente es parte del proyecto Multi-Agent Custom Automation Engine y está sujeto a los términos de la licencia del proyecto principal.