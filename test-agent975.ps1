# test-agent975.ps1
Write-Host "=== Test Agent975 ===" -ForegroundColor Cyan

# Token
$token = (az account get-access-token --resource https://ai.azure.com | ConvertFrom-Json).accessToken
if (-not $token) {
    Write-Host "Error: Ejecuta 'az login' primero" -ForegroundColor Red
    exit 1
}

# Request
$headers = @{
    "Authorization" = "Bearer $token"
    "Content-Type" = "application/json"
}

$body = @{
    input = @{
        code = "const Test = () => <View><Text>Hello</Text></View>"
    }
} | ConvertTo-Json

$url = "https://boatrentalfoundry-dev.services.ai.azure.com/api/projects/booking-agents/agents/Agent975/invoke"

try {
    $response = Invoke-RestMethod -Uri $url -Method Post -Headers $headers -Body $body
    Write-Host "Respuesta:" -ForegroundColor Green
    $response | ConvertTo-Json | Write-Host
} catch {
    Write-Host "Error: $_" -ForegroundColor Red
}
