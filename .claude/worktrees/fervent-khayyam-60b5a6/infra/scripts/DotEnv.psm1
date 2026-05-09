<#
.SYNOPSIS
Carga variables de entorno desde el archivo .env ubicado en la raíz del proyecto
#>
function Import-DotEnv {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $false)]
        [string]$Path = (Join-Path -Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) -ChildPath ".env")
    )

    if (-not (Test-Path $Path)) {
        Write-Warning "⚠️ Archivo .env no encontrado en $Path"
        return $false
    }

    Get-Content $Path | Where-Object {
        $_ -notmatch '^\s*(#|$)'  # Excluye comentarios y líneas vacías
    } | ForEach-Object {
        $name, $value = $_ -split '=', 2
        $value = $value.Trim('''"')
        [Environment]::SetEnvironmentVariable($name.Trim(), $value)
        Write-Verbose "✅ Variable cargada: $($name.Trim())"
    }

    Write-Host "✅ Variables de entorno cargadas desde $Path" -ForegroundColor Green
    return $true
}

Export-ModuleMember -Function Import-DotEnv
