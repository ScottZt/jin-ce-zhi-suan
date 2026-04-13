[CmdletBinding()]
param(
    [string]$PythonExe = "python",
    [string]$VenvDir = ".venv",
    [string]$BindHost = "",
    [int]$Port = 0,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

function Write-Step {
    param([string]$Message)
    Write-Host "[start] $Message" -ForegroundColor Green
}

$venvPython = Join-Path (Join-Path $projectRoot $VenvDir) "Scripts\python.exe"
if (Test-Path $venvPython) {
    $pythonCmd = $venvPython
    Write-Step "Using venv python: $venvPython"
} else {
    $pythonCmd = $PythonExe
    Write-Step "Using system python: $PythonExe"
}

if ($BindHost) {
    $env:SERVER_HOST = $BindHost
    Write-Step "SERVER_HOST=$BindHost"
}

if ($Port -gt 0) {
    $env:SERVER_PORT = "$Port"
    Write-Step "SERVER_PORT=$Port"
}

if ($NoStart) {
    Write-Step "NoStart enabled"
    exit 0
}

Write-Step "Starting server.py"
& $pythonCmd (Join-Path $projectRoot "server.py")
