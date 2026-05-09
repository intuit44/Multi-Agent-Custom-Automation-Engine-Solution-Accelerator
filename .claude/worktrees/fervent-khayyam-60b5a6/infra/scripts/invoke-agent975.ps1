# invoke-agent975.ps1
# Script completo para interactuar con Agent975 (AI Foundry)

Write-Host "=== Ejecutando flujo completo con Agent975 ===" -ForegroundColor Cyan

# 1. Obtener token para Azure Management
$token = (Get-AzAccessToken -ResourceUrl "https://management.azure.com").Token
$headers = @{
  "Authorization" = "Bearer $token"
  "Content-Type"  = "application/json"
}

# 2. Listar agentes disponibles
Write-Host "`n📌 Listando agentes..." -ForegroundColor Yellow
az rest --method get `
  --uri "$env:AI_FOUNDRY_ENDPOINT/api/projects/$env:AI_PROJECT_ID/agents" `
  --headers $headers `
  --resource "https://management.azure.com"

# 3. Crear un nuevo thread (conversación)
Write-Host "`n🧵 Creando thread..." -ForegroundColor Yellow
$threadResponse = az rest --method post `
  --uri "$env:AI_FOUNDRY_ENDPOINT/api/projects/$env:AI_PROJECT_ID/agents/$env:AI_AGENT_ID/threads" `
  --headers $headers `
  --body '{}' `
  --resource "https://management.azure.com" | ConvertFrom-Json
$threadId = $threadResponse.id
Write-Host "✅ Thread ID: $threadId" -ForegroundColor Green

# 4. Enviar mensaje (input del usuario)
$message = @{
  content = "Analiza este componente: const BoatCard = ({boat}) => <View><Text>{boat.name}</Text></View>"
  role    = "user"
} | ConvertTo-Json -Compress

Write-Host "`n✉️ Enviando mensaje..." -ForegroundColor Yellow
az rest --method post `
  --uri "$env:AI_FOUNDRY_ENDPOINT/api/projects/$env:AI_PROJECT_ID/threads/$threadId/messages" `
  --headers $headers `
  --body $message `
  --resource "https://management.azure.com"

# 5. Crear ejecución (run) con el agente
Write-Host "`n🏃 Lanzando run con Agent975..." -ForegroundColor Yellow
$runResponse = az rest --method post `
  --uri "$env:AI_FOUNDRY_ENDPOINT/api/projects/$env:AI_PROJECT_ID/threads/$threadId/runs" `
  --headers $headers `
  --body "{`"agent_id`":`"$env:AI_AGENT_ID`"}" `
  --resource "https://management.azure.com" | ConvertFrom-Json
$runId = $runResponse.id
Write-Host "🔄 Run iniciado: $runId" -ForegroundColor Green

# 6. Recuperar mensajes de respuesta
Write-Host "`n📨 Obteniendo respuestas..." -ForegroundColor Yellow
az rest --method get `
  --uri "$env:AI_FOUNDRY_ENDPOINT/api/projects/$env:AI_PROJECT_ID/threads/$threadId/messages" `
  --headers $headers `
  --resource "https://management.azure.com"