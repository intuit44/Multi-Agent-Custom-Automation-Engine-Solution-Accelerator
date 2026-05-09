<#
.SYNOPSIS
Script principal que carga variables de entorno y muestra configuración
#>

# Importar módulo
try {
  Import-Module $PSScriptRoot\DotEnv.psm1 -Force -ErrorAction Stop
}
catch {
  Write-Error "No se pudo cargar el módulo DotEnv: $_"
  exit 1
}

# Cargar variables de entorno
if (-not (Import-DotEnv -Verbose)) {
  Write-Error "No se pudieron cargar las variables de entorno"
  exit 1
}

# Mostrar variables de ejemplo
Write-Host "=== Configuración cargada ===" -ForegroundColor Cyan
Write-Host "CLIENT_ID: $($env:AZURE_CLIENT_ID)"
Write-Host "TENANT_ID: $($env:AZURE_TENANT_ID)"