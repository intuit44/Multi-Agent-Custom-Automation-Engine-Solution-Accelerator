
# simple-ai-foundry-integration.ps1
# Adaptado para uso con AIProjectClient de Azure (Node.js)

param(
    [string]$TargetPath = "C:\ProyectosSimbolicos\boat-rental-app\Multi-Agent-Custom-Automation-Engine-Solution-Accelerator"
)

Write-Host "=== Integración AI Foundry (Node.js) ===" -ForegroundColor Cyan

# Validar ruta
if (-not (Test-Path $TargetPath)) {
    Write-Host "❌ Error: No existe $TargetPath" -ForegroundColor Red
    exit 1
}

# Crear .codegpt/contexto
$contextDir = "$TargetPath\.codegpt"
if (-not (Test-Path $contextDir)) {
    New-Item -ItemType Directory -Path $contextDir -Force | Out-Null
}

$context = @{
    timestamp = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")
    version = "1.0.0"
    project = "booking-agents"
    agent = "Agent975"
    endpoint = "https://boatRentalFoundry-dev.services.ai.azure.com"
    method = "AIProjectClient (Node.js)"
}

$contextPath = "$contextDir\agents.context.json"
$context | ConvertTo-Json -Depth 5 | Out-File -FilePath $contextPath -Encoding UTF8

Write-Host "✅ Contexto creado en $contextPath" -ForegroundColor Green

# Crear archivo run-agent975.mjs
Write-Host "`n2️⃣ Creando script Node.js..." -ForegroundColor Yellow

$scriptContent = @'
import { AIProjectClient } from "@azure/ai-projects";
import { DefaultAzureCredential } from "@azure/identity";

async function runAgentConversation() {
  const project = new AIProjectClient(
    "https://boatRentalFoundry-dev.services.ai.azure.com/api/projects/booking-agents",
    new DefaultAzureCredential()
  );

  const agent = await project.agents.getAgent("asst_jjH5up8ROP1hF0sRYoNZyFNQ");
  console.log(`Retrieved agent: ${agent.name}`);

  const thread = await project.agents.threads.create();
  console.log(`Created thread, ID: ${thread.id}`);

  const message = await project.agents.messages.create(thread.id, "user", "Hello Agent");
  console.log(`Created message, ID: ${message.id}`);

  let run = await project.agents.runs.create(thread.id, agent.id);

  while (run.status === "queued" || run.status === "in_progress") {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    run = await project.agents.runs.get(thread.id, run.id);
  }

  if (run.status === "failed") {
    console.error(`Run failed: `, run.lastError);
  }

  console.log(`Run completed with status: ${run.status}`);

  const messages = await project.agents.messages.list(thread.id, { order: "asc" });

  for await (const m of messages) {
    const content = m.content.find((c) => c.type === "text" && "text" in c);
    if (content) {
      console.log(`${m.role}: ${content.text.value}`);
    }
  }
}

runAgentConversation().catch((error) => {
  console.error("An error occurred:", error);
});
'@

$scriptPath = "$TargetPath\run-agent975.mjs"
$scriptContent | Out-File -FilePath $scriptPath -Encoding UTF8
Write-Host "✅ Script JavaScript creado en $scriptPath" -ForegroundColor Green

# Crear README
Write-Host "`n3️⃣ Generando README..." -ForegroundColor Yellow

$readme = @"
# Integración AI Foundry con Agent975

## Archivos generados
- `.codegpt/agents.context.json`
- `run-agent975.mjs`

## Requisitos

```bash
npm install @azure/ai-projects @azure/identity
az login
```

## Uso

```bash
node run-agent975.mjs
```

Este script:

1. Carga el agente `Agent975` desde Azure AI Foundry
2. Crea un hilo y un mensaje inicial ("Hello Agent")
3. Ejecuta la conversación y espera su respuesta
4. Muestra los mensajes del asistente en orden
"@

$readmePath = "$TargetPath\AI_FOUNDRY_README.md"
$readme | Out-File -FilePath $readmePath -Encoding UTF8
Write-Host "✅ README actualizado" -ForegroundColor Green

# Resumen
Write-Host "`n🎯 Archivos generados:" -ForegroundColor Cyan
Write-Host "  - .codegpt\agents.context.json"
Write-Host "  - run-agent975.mjs"
Write-Host "  - AI_FOUNDRY_README.md"

Write-Host "`nEjecuta:" -ForegroundColor Yellow
Write-Host "  cd $TargetPath"
Write-Host "  node run-agent975.mjs"
