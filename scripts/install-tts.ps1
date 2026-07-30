[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$RuntimeRoot,
    [string]$SourceRoot,
    [string]$ModelId = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
    [string]$Revision = "3ac1f8ceaf2a40a954e91fd12d7975ad96d4eedb",
    [switch]$SkipModelDownload,
    [switch]$ForceBuild,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
    $RuntimeRoot = Join-Path $ProjectRoot "runtime"
}
if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
    $SourceRoot = Join-Path $ProjectRoot "third_party\qwen3tts-rs"
}
$RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
$SourceRoot = [System.IO.Path]::GetFullPath($SourceRoot)
$TtsDir = Join-Path $RuntimeRoot "tts"
$ModelsDir = Join-Path $RuntimeRoot "models\tts"
$ModelSlug = ($ModelId -split "/")[-1]
$ModelDir = Join-Path $ModelsDir $ModelSlug
$Destination = Join-Path $TtsDir "qwen3tts-rs.exe"

function Write-Step {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$Arguments = @(),
        [switch]$AllowFailure
    )
    if ($DryRun) {
        Write-Host "[dry-run] $FilePath $($Arguments -join ' ')"
        return $true
    }
    & $FilePath @Arguments
    $ok = $LASTEXITCODE -eq 0
    if (-not $ok -and -not $AllowFailure) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath"
    }
    return $ok
}

function Find-ExistingTtsBinary {
    foreach ($candidate in @(
            $Destination,
            (Join-Path $TtsDir "qwen3tts-synthesize.exe")
        )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    foreach ($name in @("qwen3tts-rs.exe", "qwen3tts-synthesize.exe")) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -ne $command) {
            return $command.Source
        }
    }
    return $null
}

function Install-Model {
    if ($SkipModelDownload) {
        Write-Host "Model download skipped. Configure HEARFLOW/QWEN3_TTS_MODEL_DIR if needed."
        return
    }
    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path $ModelDir | Out-Null
    }

    $hf = Get-Command "hf.exe" -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $hf) {
        $hf = Get-Command "hf" -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
    }
    if ($null -ne $hf) {
        Invoke-Checked $hf.Source @(
            "download",
            $ModelId,
            "--local-dir",
            $ModelDir
        ) | Out-Null
        return
    }

    $legacy = Get-Command "huggingface-cli.exe" -CommandType Application `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $legacy) {
        $legacy = Get-Command "huggingface-cli" -CommandType Application `
            -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    if ($null -ne $legacy) {
        Invoke-Checked $legacy.Source @(
            "download",
            $ModelId,
            "--local-dir",
            $ModelDir,
            "--local-dir-use-symlinks",
            "False"
        ) | Out-Null
        return
    }

    throw @"
找不到 Hugging Face CLI。請先安裝：
  py -m pip install -U "huggingface_hub[cli]"
然後重新執行此安裝器，或使用 -SkipModelDownload 並自行放入：
  $ModelDir
"@
}

Write-Step "Checking NVIDIA CUDA toolchain"
if ($null -eq (Get-Command "cargo.exe" -ErrorAction SilentlyContinue)) {
    throw "找不到 cargo.exe；請先安裝 Rust stable x86_64-pc-windows-msvc。"
}
if ($null -eq (Get-Command "git.exe" -ErrorAction SilentlyContinue)) {
    throw "找不到 git.exe；無法取得 qwen3tts-rs。"
}
if ($null -eq (Get-Command "nvcc.exe" -ErrorAction SilentlyContinue)) {
    throw "找不到 nvcc.exe；請安裝 NVIDIA CUDA Toolkit 並重新開啟 PowerShell。"
}

$existing = Find-ExistingTtsBinary
if ($null -ne $existing -and -not $ForceBuild) {
    Write-Step "Using existing qwen3tts-rs binary"
    Write-Host $existing
    if (-not $DryRun -and $existing -ne $Destination) {
        New-Item -ItemType Directory -Force -Path $TtsDir | Out-Null
        Copy-Item -LiteralPath $existing -Destination $Destination -Force
    }
}
else {
    Write-Step "Preparing qwen3tts-rs source"
    if (-not (Test-Path -LiteralPath $SourceRoot)) {
        Invoke-Checked "git.exe" @(
            "clone",
            "https://github.com/stevenke1981/qwen3tts-rs.git",
            $SourceRoot
        ) | Out-Null
    }
    Invoke-Checked "git.exe" @("-C", $SourceRoot, "fetch", "--tags", "origin") | Out-Null
    Invoke-Checked "git.exe" @("-C", $SourceRoot, "checkout", "--detach", $Revision) | Out-Null

    if (-not $DryRun) {
        $actual = (& git.exe -C $SourceRoot rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0 -or $actual -ne $Revision) {
            throw "qwen3tts-rs revision mismatch: actual=$actual expected=$Revision"
        }
    }

    Write-Step "Building CUDA version (--features candle-llm,cuda)"
    $built = Invoke-Checked "cargo.exe" @(
        "build",
        "--release",
        "--locked",
        "--manifest-path",
        (Join-Path $SourceRoot "Cargo.toml"),
        "--features",
        "candle-llm cuda",
        "--bin",
        "qwen3tts-rs"
    ) -AllowFailure

    $candidates = @(
        (Join-Path $SourceRoot "target\release\qwen3tts-rs.exe"),
        (Join-Path $SourceRoot "target\release\qwen3tts-synthesize.exe")
    )
    if (-not $DryRun -and -not ($candidates | Where-Object { Test-Path $_ -PathType Leaf })) {
        Write-Step "Binary target not present; building synthesize example"
        Invoke-Checked "cargo.exe" @(
            "build",
            "--release",
            "--locked",
            "--manifest-path",
            (Join-Path $SourceRoot "Cargo.toml"),
            "--features",
            "candle-llm cuda",
            "--example",
            "synthesize"
        ) | Out-Null
        $candidates += (Join-Path $SourceRoot "target\release\examples\synthesize.exe")
    }

    if (-not $DryRun) {
        $binary = $candidates | Where-Object { Test-Path $_ -PathType Leaf } |
            Select-Object -First 1
        if ($null -eq $binary) {
            throw "CUDA build completed but no qwen3tts-rs/synthesize executable was found."
        }
        New-Item -ItemType Directory -Force -Path $TtsDir | Out-Null
        Copy-Item -LiteralPath $binary -Destination $Destination -Force
    }
}

Write-Step "Installing Qwen3-TTS model"
Install-Model

Write-Step "Verifying installation"
if (-not $DryRun) {
    Invoke-Checked $Destination @("--help") | Out-Null
    $manifest = [ordered]@{
        schema_version = 1
        installed_at_utc = [DateTime]::UtcNow.ToString("o")
        engine = "qwen3tts-rs"
        revision = $Revision
        cuda = $true
        executable = $Destination
        model_id = $ModelId
        model_dir = $ModelDir
        contains_credentials = $false
    }
    $json = $manifest | ConvertTo-Json -Depth 5
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        (Join-Path $TtsDir "manifest.json"),
        $json,
        $utf8
    )
}

Write-Host ""
Write-Host "Qwen3-TTS CUDA runtime is ready." -ForegroundColor Green
Write-Host "Executable: $Destination"
Write-Host "Model:      $ModelDir"
