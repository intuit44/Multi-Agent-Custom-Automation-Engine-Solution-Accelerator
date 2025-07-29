# IntegraciÃ³n AI Foundry con Agent975

## Archivos generados
- .codegpt/agents.context.json
- un-agent975.mjs

## Requisitos

`ash
npm install @azure/ai-projects @azure/identity
az login
`

## Uso

`ash
node run-agent975.mjs
`

Este script:

1. Carga el agente Agent975 desde Azure AI Foundry
2. Crea un hilo y un mensaje inicial ("Hello Agent")
3. Ejecuta la conversaciÃ³n y espera su respuesta
4. Muestra los mensajes del asistente en orden
