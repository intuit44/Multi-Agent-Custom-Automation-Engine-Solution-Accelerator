# deploy_agents_endpoints_fixed.ps1

# 1. Configuración inicial
$env:AI_FOUNDRY_ENDPOINT = "https://boatRentalFoundry-dev.services.ai.azure.com"
$env:AI_PROJECT_ID = "booking-agents"
$env:AI_AGENT_ID = "asst_jjH5up8ROP1hF0sRYoNZyFNQ"

# Ruta relativa para el archivo de token
$ScriptDir = Split-Path -Path $MyInvocation.MyCommand.Definition -Parent
$TokenFile = Join-Path -Path $ScriptDir -ChildPath "generated_token.json"

# 2. Función para verificar y obtener el token
function Get-AzureToken {
  param (
    [string]$TokenFile
  )

  # Verificar si el archivo de token existe
  if (Test-Path $TokenFile) {
    $tokenData = Get-Content $TokenFile | ConvertFrom-Json
    $expiryTime = [datetime]::Parse($tokenData.expires_on)

    # Verificar si el token ha expirado
    if ($expiryTime -gt (Get-Date)) {
      Write-Host "`n=== [$(Get-Date -Format HH:mm:ss)] Token válido encontrado. Reutilizando..." -ForegroundColor Green
      return $tokenData.access_token
    }
  }

  # Si no hay token válido, ejecutar generar_token_azure.ps1
  Write-Host "`n=== [$(Get-Date -Format HH:mm:ss)] Token no válido o no encontrado. Generando nuevo token..." -ForegroundColor Yellow
  & "$ScriptDir\generar_token_azure.ps1"

  # Leer el nuevo token generado
  $tokenData = Get-Content $TokenFile | ConvertFrom-Json
  return $tokenData.access_token
}

# 3. Obtener el token
$token = Get-AzureToken -TokenFile $TokenFile
$headers = @{
  "Authorization" = "Bearer $token"
  "Content-Type"  = "application/json"
  "api-version"   = "2023-10-01"
}

# 4. Función para manejar errores
function Invoke-AIFoundryRequest {
  param (
    [string]$Uri,
    [string]$Method = "GET",
    [object]$Body = $null
  )
    
  try {
    $bodyJson = if ($Body) { $Body | ConvertTo-Json -Depth 5 -Compress } else { $null }
    $response = Invoke-RestMethod -Uri $Uri -Method $Method -Headers $headers -Body $bodyJson
    if (-not $response) {
      Write-Host "`n=== [$(Get-Date -Format HH:mm:ss)] La API devolvió una respuesta nula para $Uri ===" -ForegroundColor Yellow
      return $null
    }
    return $response
  }
  catch {
    Write-Host "`n=== [$(Get-Date -Format HH:mm:ss)] Error en la solicitud: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Detalles: $($_.ErrorDetails.Message)" -ForegroundColor Yellow
    return $null
  }
}

# 5. Listar agentes
Write-Host "`n=== [$(Get-Date -Format HH:mm:ss)] Listando agentes ===" -ForegroundColor Cyan
$agents = Invoke-AIFoundryRequest -Uri "$env:AI_FOUNDRY_ENDPOINT/api/projects/$env:AI_PROJECT_ID/agents"
if ($agents) {
  Write-Host ($agents | ConvertTo-Json -Depth 3)
}
else {
  Write-Host "`n=== [$(Get-Date -Format HH:mm:ss)] No se pudieron listar los agentes ===" -ForegroundColor Yellow
}

# 6. Crear thread
Write-Host "`n=== [$(Get-Date -Format HH:mm:ss)] Creando thread ===" -ForegroundColor Cyan
$thread = Invoke-AIFoundryRequest -Uri "$env:AI_FOUNDRY_ENDPOINT/api/projects/$env:AI_PROJECT_ID/agents/$env:AI_AGENT_ID/threads" -Method POST
if ($thread) {
  Write-Host "Thread creado: $($thread.id)"
}
else {
  Write-Host "`n=== [$(Get-Date -Format HH:mm:ss)] No se pudo crear el thread ===" -ForegroundColor Yellow
}

# 7. Enviar mensaje
Write-Host "`n=== [$(Get-Date -Format HH:mm:ss)] Enviando mensaje ===" -ForegroundColor Cyan
$message = @{
  content = "Analiza este componente: const BoatCard = ({boat}) => <View><Text>{boat.name}</Text></View>"
  role    = "user"
}
$response = Invoke-AIFoundryRequest -Uri "$env:AI_FOUNDRY_ENDPOINT/api/projects/$env:AI_PROJECT_ID/threads/$($thread.id)/messages" -Method POST -Body $message
if ($response) {
  Write-Host "Mensaje enviado: $($response.id)"
}
else {
  Write-Host "`n=== [$(Get-Date -Format HH:mm:ss)] No se pudo enviar el mensaje ===" -ForegroundColor Yellow
}

# 8. Ejecutar agente
Write-Host "`n=== [$(Get-Date -Format HH:mm:ss)] Ejecutando agente ===" -ForegroundColor Cyan
$run = @{
  agent_id = $env:AI_AGENT_ID
}
$response = Invoke-AIFoundryRequest -Uri "$env:AI_FOUNDRY_ENDPOINT/api/projects/$env:AI_PROJECT_ID/threads/$($thread.id)/runs" -Method POST -Body $run
if ($response) {
  Write-Host "Run iniciado: $($response.id)"
}
else {
  Write-Host "`n=== [$(Get-Date -Format HH:mm:ss)] No se pudo iniciar el run ===" -ForegroundColor Yellow
}

# 9. Obtener mensajes
Write-Host "`n=== [$(Get-Date -Format HH:mm:ss)] Obteniendo mensajes ===" -ForegroundColor Cyan
Start-Sleep -Seconds 5  # Esperar procesamiento
$messages = Invoke-AIFoundryRequest -Uri "$env:AI_FOUNDRY_ENDPOINT/api/projects/$env:AI_PROJECT_ID/threads/$($thread.id)/messages" -Method GET
if ($messages) {
  Write-Host "Respuestas recibidas:"
  Write-Host ($messages | ConvertTo-Json -Depth 5)
}
else {
  Write-Host "`n=== [$(Get-Date -Format HH:mm:ss)] No se pudieron obtener mensajes ===" -ForegroundColor Yellow
}