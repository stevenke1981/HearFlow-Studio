[CmdletBinding()]
param(
    [switch]$SkipUpstream
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Venv = Join-Path $Root ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    python -m venv $Venv
}

& $Python -m pip install --disable-pip-version-check -r (Join-Path $Root "requirements-dev.txt")

Push-Location $Root
try {
    & $Python -m compileall -q hearflow tests
    & $Python -m ruff check hearflow tests
    & $Python -m pytest
}
finally {
    Pop-Location
}

if (-not $SkipUpstream) {
    $Upstream = Join-Path $Root "third_party\qwen3-asr-llama-cpp"
    & (Join-Path $Upstream "scripts\validate-package.ps1")

    Push-Location (Join-Path $Upstream "gateway")
    try {
        cargo fmt --all -- --check
        cargo check --all-targets --locked
        cargo test --all-targets --locked
    }
    finally {
        Pop-Location
    }
}

Write-Host "HearFlow verification passed."

