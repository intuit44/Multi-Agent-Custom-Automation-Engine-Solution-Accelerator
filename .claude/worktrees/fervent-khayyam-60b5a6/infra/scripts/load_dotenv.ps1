<#
.SYNOPSIS
Carga variables de entorno desde un archivo .env
#>
function Load-DotEnv {
  [CmdletBinding()]
  param (
    [Parameter(Mandatory = $false)]
    [string]$Path = ".\.env"
  )

  if (-not (Test-Path $Path)) {
    Write-Warning "Archivo .env no encontrado en $Path"
    return
  }

  Get-Content $Path | ForEach-Object {
    if ($_ -match '^\s*#') { return }  # Ignorar comentarios
    if ($_ -match '^\s*$') { return }  # Ignorar líneas vacías
        
    if ($_ -match '^\s*([^=]+)\s*=\s*(.*)\s*$') {
      $name = $matches[1].Trim()
      $value = $matches[2].Trim()
            
      # Remover comillas si existen
      if ($value -match '^"(.*)"$') { $value = $matches[1] }
      elseif ($value -match "^'(.*)'$") { $value = $matches[1] }
            
      # Establecer variable de entorno
      [System.Environment]::SetEnvironmentVariable($name, $value)
      Write-Verbose "Cargada variable: $name"
    }
  }

  Write-Host "✅ Variables de entorno cargadas desde $Path" -ForegroundColor Green
}

Export-ModuleMember -Function Load-DotEnv