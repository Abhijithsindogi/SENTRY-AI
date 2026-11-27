[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'The project virtual environment is missing. See README.md for setup.'
}

$base = "http://127.0.0.1:$Port"
try {
    $existing = Invoke-RestMethod "$base/openapi.json" -TimeoutSec 2
} catch {
    $existing = $null
}
if ($existing.info.title -like 'SENTRY AI*') {
    Write-Host "SENTRY AI already responds at $base. Leaving the current process running."
    return
}

Push-Location -LiteralPath $PSScriptRoot
try {
    Write-Host "SENTRY AI dashboard at $base. Press Ctrl+C to stop this server."
    & $python -m uvicorn engine:app --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
