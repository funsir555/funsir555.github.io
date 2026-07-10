$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path ".venv")) {
    throw ".venv not found. Run .\\bootstrap_local.ps1 first."
}

if (-not (Test-Path ".env")) {
    throw ".env not found. Run .\\bootstrap_local.ps1 first."
}

Get-Content ".env" | ForEach-Object {
    $line = $_.Trim()
    if (-not $line) { return }
    if ($line.StartsWith("#")) { return }
    $parts = $line.Split("=", 2)
    if ($parts.Count -ne 2) { return }
    [System.Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process")
}

if (-not $env:LICENSE_DB_PATH) {
    $env:LICENSE_DB_PATH = ".\data\license.db"
}

& ".\.venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8000
