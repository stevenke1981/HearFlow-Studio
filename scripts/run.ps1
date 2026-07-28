[CmdletBinding()]
param(
    [switch]$NoInstall
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Venv = Join-Path $Root ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    python -m venv $Venv
}

if (-not $NoInstall) {
    & $Python -m pip install --disable-pip-version-check -r (Join-Path $Root "requirements.txt")
}

Push-Location $Root
try {
    & $Python -m hearflow.app
}
finally {
    Pop-Location
}

