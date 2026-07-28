[CmdletBinding()]
param(
    [switch]$SkipTests,

    [switch]$SkipEngineRuntime,

    [string]$Version = "0.1.0",

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Venv = Join-Path $Root ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$Dist = Join-Path $Root "dist"
$Runtime = Join-Path $Root "runtime"

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $stream = [System.IO.File]::OpenRead($LiteralPath)
    try {
        $algorithm = [System.Security.Cryptography.SHA256]::Create()
        try {
            $bytes = $algorithm.ComputeHash($stream)
        }
        finally {
            $algorithm.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
    return ([System.BitConverter]::ToString($bytes)).Replace("-", "").ToLowerInvariant()
}

if ($Version -notmatch "^[0-9A-Za-z][0-9A-Za-z._-]*$") {
    throw "Version contains characters that are unsafe for a release file name: $Version"
}

function Assert-EngineRuntime {
    $RuntimeManifest = Join-Path $Runtime "manifest.json"
    if (-not (Test-Path -LiteralPath $RuntimeManifest -PathType Leaf)) {
        throw @"
The managed engine runtime is not installed. Run:
  .\scripts\install-engine.ps1 -Backend cpu
or explicitly build an app-only archive with -SkipEngineRuntime.
"@
    }

    foreach ($runtimeDirectory in @("engine", "gateway", "models", "ffmpeg")) {
        $source = Join-Path $Runtime $runtimeDirectory
        if (-not (Test-Path -LiteralPath $source -PathType Container)) {
            throw "Runtime manifest exists, but required directory is missing: $source"
        }
    }

    $manifest = Get-Content -LiteralPath $RuntimeManifest -Raw | ConvertFrom-Json
    $requiredFiles = @("llama_server", "gateway", "model", "mmproj", "ffmpeg", "ffprobe")
    $runtimePrefix = [System.IO.Path]::GetFullPath($Runtime).TrimEnd("\") + "\"
    foreach ($name in $requiredFiles) {
        $record = $manifest.files.$name
        if ($null -eq $record -or [string]::IsNullOrWhiteSpace($record.path)) {
            throw "Runtime manifest has no file record for '$name'."
        }
        $fullPath = [System.IO.Path]::GetFullPath(
            (Join-Path $Runtime ([string]$record.path).Replace("/", "\"))
        )
        if (-not $fullPath.StartsWith($runtimePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Runtime manifest path escapes the runtime root: $($record.path)"
        }
        if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
            throw "Runtime file is missing: $fullPath"
        }
        $actualHash = Get-Sha256 -LiteralPath $fullPath
        if ($actualHash -ne ([string]$record.sha256).ToLowerInvariant()) {
            throw "Runtime SHA-256 mismatch: $($record.path)"
        }
    }

    return $manifest
}

if ($DryRun) {
    foreach ($requiredSource in @(
        "hearflow\app.py",
        "hearflow\ui\theme.qss",
        "requirements-dev.txt",
        "README.md",
        "THIRD_PARTY_NOTICES.md",
        "docs\INSTALLATION.md",
        "docs\ACCEPTANCE.md"
    )) {
        $path = Join-Path $Root $requiredSource
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Build input is missing: $path"
        }
    }

    if (-not $SkipEngineRuntime) {
        $checkedManifest = Assert-EngineRuntime
        Write-Host "Runtime verified: $($checkedManifest.backend), $($checkedManifest.architecture)"
    }
    Write-Host "[dry-run] PyInstaller one-folder application: dist\HearFlowStudio"
    Write-Host "[dry-run] Portable archive: release\HearFlowStudio-$Version-win-x64-portable.zip"
    Write-Host "[dry-run] Engine runtime included: $(-not $SkipEngineRuntime)"
    Write-Host "Dry run completed; no build output was changed." -ForegroundColor Green
    return
}

if (-not (Test-Path -LiteralPath $Python)) {
    python -m venv $Venv
}

& $Python -m pip install --disable-pip-version-check -r (Join-Path $Root "requirements-dev.txt")

if (-not $SkipTests) {
    & (Join-Path $Root "scripts\verify.ps1") -SkipUpstream
}

Push-Location $Root
try {
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --windowed `
        --noupx `
        --name HearFlowStudio `
        --add-data "hearflow\ui\theme.qss;hearflow\ui" `
        --collect-all keyring `
        --collect-submodules hearflow `
        "hearflow\app.py"

    $AppDist = Join-Path $Dist "HearFlowStudio"
    $Licenses = Join-Path $AppDist "licenses"
    New-Item -ItemType Directory -Force -Path $Licenses | Out-Null
    Copy-Item -LiteralPath (Join-Path $Root "LICENSE") -Destination (Join-Path $Licenses "HearFlow-MIT.txt") -Force
    Copy-Item -LiteralPath (Join-Path $Root "THIRD_PARTY_NOTICES.md") -Destination $AppDist -Force
    Copy-Item -LiteralPath (Join-Path $Root "README.md") -Destination $AppDist -Force
    Copy-Item -LiteralPath (Join-Path $Root "docs\INSTALLATION.md") -Destination $AppDist -Force
    Copy-Item -LiteralPath (Join-Path $Root "docs\ACCEPTANCE.md") -Destination $AppDist -Force

    $PackagedScripts = Join-Path $AppDist "scripts"
    New-Item -ItemType Directory -Force -Path $PackagedScripts | Out-Null
    Copy-Item -LiteralPath (Join-Path $Root "scripts\install-engine.ps1") `
        -Destination $PackagedScripts -Force

    $UpstreamLicense = Join-Path $Root "third_party\qwen3-asr-llama-cpp\LICENSE"
    if (Test-Path -LiteralPath $UpstreamLicense) {
        Copy-Item -LiteralPath $UpstreamLicense -Destination (Join-Path $Licenses "qwen3-asr-gateway-MIT.txt") -Force
    }

    if (-not $SkipEngineRuntime) {
        $null = Assert-EngineRuntime
        $RuntimeManifest = Join-Path $Runtime "manifest.json"

        $PortableRuntime = Join-Path $AppDist "runtime"
        New-Item -ItemType Directory -Force -Path $PortableRuntime | Out-Null
        Copy-Item -LiteralPath $RuntimeManifest -Destination $PortableRuntime -Force

        foreach ($runtimeDirectory in @("engine", "gateway", "models", "ffmpeg")) {
            $source = Join-Path $Runtime $runtimeDirectory
            Copy-Item -LiteralPath $source -Destination $PortableRuntime -Recurse -Force
        }

        Get-ChildItem -LiteralPath $PortableRuntime -Recurse -File |
            Where-Object {
                $_.Name -match "^(LICENSE|COPYING|NOTICE|README\.(Qwen3-ASR|ffmpeg))"
            } |
            ForEach-Object {
                $licenseName = ($_.FullName.Substring($PortableRuntime.Length).TrimStart("\") `
                    -replace "[\\/:*?`"<>|]", "_")
                Copy-Item -LiteralPath $_.FullName `
                    -Destination (Join-Path $Licenses $licenseName) -Force
            }

        $ForbiddenRuntimeItems = @("downloads", "logs", "e2e", "install-staging")
        foreach ($forbidden in $ForbiddenRuntimeItems) {
            if (Test-Path -LiteralPath (Join-Path $PortableRuntime $forbidden)) {
                throw "Packaging invariant violated: runtime\$forbidden must not be included."
            }
        }
    }

    $ReleaseDir = Join-Path $Root "release"
    New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
    $ZipName = "HearFlowStudio-$Version-win-x64-portable.zip"
    $Zip = Join-Path $ReleaseDir $ZipName
    if (Test-Path -LiteralPath $Zip) {
        Remove-Item -LiteralPath $Zip -Force
    }
    Compress-Archive -Path (Join-Path $AppDist "*") -DestinationPath $Zip -CompressionLevel Optimal

    $ZipHash = Get-Sha256 -LiteralPath $Zip
    $HashFile = "$Zip.sha256"
    "$ZipHash  $ZipName" | Set-Content -LiteralPath $HashFile -Encoding ascii

    $ReleaseManifest = [ordered]@{
        schema_version = 1
        product = "HearFlow Studio"
        version = $Version
        built_at_utc = [DateTime]::UtcNow.ToString("o")
        archive = $ZipName
        sha256 = $ZipHash
        engine_runtime_included = -not $SkipEngineRuntime
    }
    $ReleaseManifest |
        ConvertTo-Json -Depth 4 |
        Out-File -LiteralPath (Join-Path $ReleaseDir "release-manifest.json") -Encoding utf8
}
finally {
    Pop-Location
}

Write-Host "Build output: $Dist\HearFlowStudio\HearFlowStudio.exe"
Write-Host "Portable archive: $Zip"
Write-Host "SHA-256: $ZipHash"
