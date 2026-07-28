[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateSet("cpu", "cuda12.4", "vulkan")]
    [string]$Backend = "cpu",

    [string]$RuntimeRoot,

    [switch]$SkipModelDownload,

    [switch]$SkipGatewayBuild,

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
    $RuntimeRoot = Join-Path $ProjectRoot "runtime"
}
$RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)

$LlamaRevision = "b10155"
$GatewayRevision = "363b60618e4029977d6492d4f41d852638740a43"
$ModelRepository = "ggml-org/Qwen3-ASR-0.6B-GGUF"
$ModelFileName = "Qwen3-ASR-0.6B-Q8_0.gguf"
$MmprojFileName = "mmproj-Qwen3-ASR-0.6B-Q8_0.gguf"

$ExpectedSha256 = @{
    "llama-b10155-bin-win-cpu-x64.zip" = "1f38b45dd037844145fecf2bffe2e85b49798d8ec0b3ab8abec1314e01277fed"
    "llama-b10155-bin-win-cuda-12.4-x64.zip" = "92b60adc8a8895c52b3e1939e025a4fa742470de37492dd850218a8faa9176e5"
    "cudart-llama-bin-win-cuda-12.4-x64.zip" = "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6"
    "llama-b10155-bin-win-vulkan-x64.zip" = "d9d6c72ab8922123b7fb040b4178105e96f15e296cc4b6c3153b938a1c7ff0b4"
    $ModelFileName = "bca259818b50ca7c4c05e9bdb35a5dc04fa039653a6d6f3f0f331f96f6aa1971"
    $MmprojFileName = "41a342b5e4c514e968cb756de6cd1b7be39eff43c44c57a2ef5fc6522e36603d"
}

$AssetByBackend = @{
    "cpu" = @("llama-b10155-bin-win-cpu-x64.zip")
    "cuda12.4" = @(
        "llama-b10155-bin-win-cuda-12.4-x64.zip",
        "cudart-llama-bin-win-cuda-12.4-x64.zip"
    )
    "vulkan" = @("llama-b10155-bin-win-vulkan-x64.zip")
}

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message" -ForegroundColor Cyan
}

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

function Get-RelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$BasePath,
        [Parameter(Mandatory = $true)][string]$TargetPath
    )

    $trimChars = [char[]]@("\", "/")
    $base = [System.IO.Path]::GetFullPath($BasePath).TrimEnd($trimChars) + "\"
    $target = [System.IO.Path]::GetFullPath($TargetPath)
    if (-not $target.StartsWith($base, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside the expected base directory: $target"
    }
    return $target.Substring($base.Length)
}

function Assert-NoReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)

    $trimChars = [char[]]@("\", "/")
    $runtime = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd($trimChars)
    $candidate = [System.IO.Path]::GetFullPath($Path).TrimEnd($trimChars)
    if (
        -not $candidate.Equals($runtime, [System.StringComparison]::OrdinalIgnoreCase) -and
        -not $candidate.StartsWith($runtime + "\", [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "Installer path is outside the runtime root: $candidate"
    }

    while ($candidate.Length -ge $runtime.Length) {
        if (Test-Path -LiteralPath $candidate) {
            $item = Get-Item -LiteralPath $candidate -Force
            if (
                [int]($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
            ) {
                throw "Installer refuses a junction or symbolic link in the runtime path: $candidate"
            }
        }
        if ($candidate.Equals($runtime, [System.StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $parent = Split-Path -Parent $candidate
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $candidate) {
            break
        }
        $candidate = $parent.TrimEnd($trimChars)
    }
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory)]
        [string]$FilePath,
        [string[]]$Arguments = @()
    )

    if ($DryRun) {
        Write-Host "[dry-run] $FilePath $($Arguments -join ' ')"
        return
    }

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath"
    }
}

function Invoke-Download {
    param(
        [Parameter(Mandatory)]
        [string]$Uri,
        [Parameter(Mandatory)]
        [string]$Destination,
        [string]$ExpectedHash
    )

    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        if (-not [string]::IsNullOrWhiteSpace($ExpectedHash)) {
            $cachedHash = Get-Sha256 -LiteralPath $Destination
            if ($cachedHash -ne $ExpectedHash) {
                throw @"
Cached download SHA-256 mismatch: $Destination
Expected: $ExpectedHash
Actual:   $cachedHash
Remove only this cached file and rerun the installer.
"@
            }
        }
        Write-Host "Using cached download: $Destination"
        return
    }
    if ($DryRun) {
        Write-Host "[dry-run] Download $Uri -> $Destination"
        return
    }

    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $partial = "$Destination.partial"
    if (Test-Path -LiteralPath $partial) {
        Remove-Item -LiteralPath $partial -Force
    }

    try {
        $bits = Get-Command Start-BitsTransfer -ErrorAction SilentlyContinue
        if ($null -ne $bits) {
            Start-BitsTransfer -Source $Uri -Destination $partial -DisplayName "HearFlow engine download"
        }
        else {
            Invoke-WebRequest -Uri $Uri -OutFile $partial -UseBasicParsing
        }
        if (-not [string]::IsNullOrWhiteSpace($ExpectedHash)) {
            $downloadedHash = Get-Sha256 -LiteralPath $partial
            if ($downloadedHash -ne $ExpectedHash) {
                throw @"
Downloaded file SHA-256 mismatch: $Uri
Expected: $ExpectedHash
Actual:   $downloadedHash
"@
            }
        }
        Move-Item -LiteralPath $partial -Destination $Destination -Force
    }
    catch {
        if (Test-Path -LiteralPath $partial) {
            Remove-Item -LiteralPath $partial -Force
        }
        throw
    }
}

function Resolve-ExecutableSource {
    param([Parameter(Mandatory)][string]$Name)

    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $command) {
        return $null
    }

    $item = Get-Item -LiteralPath $command.Source
    if ($item.LinkType -and $item.Target) {
        $target = [string]($item.Target | Select-Object -First 1)
        if (-not [System.IO.Path]::IsPathRooted($target)) {
            $target = Join-Path $item.DirectoryName $target
        }
        return [System.IO.Path]::GetFullPath($target)
    }
    return $item.FullName
}

function Copy-SystemFFmpeg {
    param([Parameter(Mandatory)][string]$Destination)

    $ffmpeg = Resolve-ExecutableSource "ffmpeg.exe"
    $ffprobe = Resolve-ExecutableSource "ffprobe.exe"
    if (-not $ffmpeg -or -not $ffprobe) {
        throw @"
FFmpeg/FFprobe were not found. Install a Windows FFmpeg build and make both
executables available on PATH, then rerun this installer.
"@
    }

    if ($DryRun) {
        Write-Host "[dry-run] Copy FFmpeg from $ffmpeg and $ffprobe -> $Destination"
        return
    }

    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Copy-Item -LiteralPath $ffmpeg -Destination (Join-Path $Destination "ffmpeg.exe") -Force
    Copy-Item -LiteralPath $ffprobe -Destination (Join-Path $Destination "ffprobe.exe") -Force

    $sourceDirectory = Split-Path -Parent $ffmpeg
    Get-ChildItem -LiteralPath $sourceDirectory -Filter "*.dll" -File -ErrorAction SilentlyContinue |
        ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $Destination -Force
        }

    $distributionRoot = Split-Path -Parent $sourceDirectory
    $ffmpegLicense = Join-Path $distributionRoot "LICENSE"
    $ffmpegReadme = Join-Path $distributionRoot "README.txt"
    if (Test-Path -LiteralPath $ffmpegLicense -PathType Leaf) {
        Copy-Item -LiteralPath $ffmpegLicense `
            -Destination (Join-Path $Destination "LICENSE.ffmpeg-build.txt") -Force
    }
    if (Test-Path -LiteralPath $ffmpegReadme -PathType Leaf) {
        Copy-Item -LiteralPath $ffmpegReadme `
            -Destination (Join-Path $Destination "README.ffmpeg-build.txt") -Force
    }

    Invoke-Checked (Join-Path $Destination "ffmpeg.exe") @("-version")
    Invoke-Checked (Join-Path $Destination "ffprobe.exe") @("-version")
}

function Get-FileRecord {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    $item = Get-Item -LiteralPath $Path
    return [ordered]@{
        path = (Get-RelativePath -BasePath $RuntimeRoot -TargetPath $item.FullName).Replace("\", "/")
        bytes = $item.Length
        sha256 = Get-Sha256 -LiteralPath $item.FullName
    }
}

Write-Step "Preparing HearFlow runtime ($Backend)"
if (-not $DryRun) {
    New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
}

$Downloads = Join-Path $RuntimeRoot "downloads"
$EngineDir = Join-Path $RuntimeRoot "engine\$Backend"
$GatewayDir = Join-Path $RuntimeRoot "gateway"
$ModelsDir = Join-Path $RuntimeRoot "models"
$FFmpegDir = Join-Path $RuntimeRoot "ffmpeg"
$StagingRoot = Join-Path $RuntimeRoot ("install-staging\" + [guid]::NewGuid().ToString("N"))

foreach ($installerPath in @(
        $RuntimeRoot,
        $Downloads,
        $EngineDir,
        $GatewayDir,
        $ModelsDir,
        $FFmpegDir,
        $StagingRoot
    )) {
    Assert-NoReparsePoint -Path $installerPath
}

try {
    Write-Step "Installing llama.cpp $LlamaRevision"
    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path $Downloads, $EngineDir, $StagingRoot | Out-Null
    }

    foreach ($asset in $AssetByBackend[$Backend]) {
        $assetUrl = "https://github.com/ggml-org/llama.cpp/releases/download/$LlamaRevision/$asset"
        $archive = Join-Path $Downloads $asset
        Invoke-Download `
            -Uri $assetUrl `
            -Destination $archive `
            -ExpectedHash $ExpectedSha256[$asset]
        if (-not $DryRun) {
            $assetStaging = Join-Path $StagingRoot ([System.IO.Path]::GetFileNameWithoutExtension($asset))
            New-Item -ItemType Directory -Force -Path $assetStaging | Out-Null
            Expand-Archive -LiteralPath $archive -DestinationPath $assetStaging -Force
            Get-ChildItem -LiteralPath $assetStaging -File -Recurse | ForEach-Object {
                $relative = Get-RelativePath -BasePath $assetStaging -TargetPath $_.FullName
                $target = Join-Path $EngineDir $relative
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
                Copy-Item -LiteralPath $_.FullName -Destination $target -Force
            }
        }
    }

    Invoke-Download `
        -Uri "https://raw.githubusercontent.com/ggml-org/llama.cpp/$LlamaRevision/LICENSE" `
        -Destination (Join-Path $EngineDir "LICENSE.llama.cpp.txt")

    $llamaServer = Join-Path $EngineDir "llama-server.exe"
    if (-not $DryRun -and -not (Test-Path -LiteralPath $llamaServer -PathType Leaf)) {
        $nestedServer = Get-ChildItem -LiteralPath $EngineDir -Filter "llama-server.exe" -File -Recurse |
            Select-Object -First 1
        if ($null -eq $nestedServer) {
            throw "llama-server.exe was not found after extracting llama.cpp."
        }
        Copy-Item -LiteralPath $nestedServer.FullName -Destination $llamaServer -Force
    }

    Write-Step "Building the pinned Qwen3-ASR Gateway"
    $UpstreamRoot = Join-Path $ProjectRoot "third_party\qwen3-asr-llama-cpp"
    if (-not (Test-Path -LiteralPath $UpstreamRoot)) {
        if ($DryRun) {
            Write-Host "[dry-run] Clone qwen3-asr-llama-cpp at $GatewayRevision -> $UpstreamRoot"
        }
        else {
            Invoke-Checked "git.exe" @(
                "clone",
                "https://github.com/stevenke1981/qwen3-asr-llama-cpp.git",
                $UpstreamRoot
            )
            Invoke-Checked "git.exe" @("-C", $UpstreamRoot, "checkout", "--detach", $GatewayRevision)
        }
    }

    if (-not $DryRun) {
        $actualRevision = (& git.exe -C $UpstreamRoot rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to inspect the vendored gateway revision."
        }
        if ($actualRevision -ne $GatewayRevision) {
            throw "Gateway source is $actualRevision; expected pinned revision $GatewayRevision."
        }
    }

    $gatewayBinary = Join-Path $UpstreamRoot "gateway\target\release\qwen3-asr-gateway.exe"
    if (-not $SkipGatewayBuild) {
        Invoke-Checked "cargo.exe" @(
            "build",
            "--release",
            "--locked",
            "--manifest-path",
            (Join-Path $UpstreamRoot "gateway\Cargo.toml")
        )
    }
    if (-not $DryRun) {
        if (-not (Test-Path -LiteralPath $gatewayBinary -PathType Leaf)) {
            throw "Gateway executable is missing. Install Rust or rerun without -SkipGatewayBuild."
        }
        New-Item -ItemType Directory -Force -Path $GatewayDir | Out-Null
        Copy-Item -LiteralPath $gatewayBinary -Destination (Join-Path $GatewayDir "qwen3-asr-gateway.exe") -Force
    }

    Write-Step "Locating FFmpeg and FFprobe"
    $packagedFFmpeg = Get-ChildItem -LiteralPath $EngineDir -Filter "ffmpeg.exe" -File -Recurse `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    $packagedFFprobe = Get-ChildItem -LiteralPath $EngineDir -Filter "ffprobe.exe" -File -Recurse `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($packagedFFmpeg -and $packagedFFprobe) {
        if ($DryRun) {
            Write-Host "[dry-run] Copy FFmpeg tools from llama.cpp package -> $FFmpegDir"
        }
        else {
            New-Item -ItemType Directory -Force -Path $FFmpegDir | Out-Null
            Copy-Item -LiteralPath $packagedFFmpeg.FullName -Destination (Join-Path $FFmpegDir "ffmpeg.exe") -Force
            Copy-Item -LiteralPath $packagedFFprobe.FullName -Destination (Join-Path $FFmpegDir "ffprobe.exe") -Force
        }
    }
    else {
        Copy-SystemFFmpeg -Destination $FFmpegDir
    }

    Write-Step "Installing Qwen3-ASR 0.6B Q8_0 model files"
    $modelPath = Join-Path $ModelsDir $ModelFileName
    $mmprojPath = Join-Path $ModelsDir $MmprojFileName
    if (-not $SkipModelDownload) {
        $modelBase = "https://huggingface.co/$ModelRepository/resolve/main"
        Invoke-Download `
            -Uri "$modelBase/${ModelFileName}?download=true" `
            -Destination $modelPath `
            -ExpectedHash $ExpectedSha256[$ModelFileName]
        Invoke-Download `
            -Uri "$modelBase/${MmprojFileName}?download=true" `
            -Destination $mmprojPath `
            -ExpectedHash $ExpectedSha256[$MmprojFileName]
    }
    else {
        if (-not (Test-Path -LiteralPath $modelPath) -or -not (Test-Path -LiteralPath $mmprojPath)) {
            throw "-SkipModelDownload requires both model files to already exist in $ModelsDir."
        }
        $modelHash = Get-Sha256 -LiteralPath $modelPath
        if ($modelHash -ne $ExpectedSha256[$ModelFileName]) {
            throw @"
Existing model SHA-256 mismatch: $modelPath
Expected: $($ExpectedSha256[$ModelFileName])
Actual:   $modelHash
"@
        }
        $mmprojHash = Get-Sha256 -LiteralPath $mmprojPath
        if ($mmprojHash -ne $ExpectedSha256[$MmprojFileName]) {
            throw @"
Existing mmproj SHA-256 mismatch: $mmprojPath
Expected: $($ExpectedSha256[$MmprojFileName])
Actual:   $mmprojHash
"@
        }
        Write-Host "Verified existing model files (download skipped)."
    }
    Invoke-Download `
        -Uri "https://huggingface.co/$ModelRepository/resolve/main/README.md" `
        -Destination (Join-Path $ModelsDir "README.Qwen3-ASR-GGUF.md")
    Invoke-Download `
        -Uri "https://huggingface.co/Qwen/Qwen3-ASR-0.6B/resolve/main/README.md" `
        -Destination (Join-Path $ModelsDir "README.Qwen3-ASR-base-model.md")

    Write-Step "Writing runtime manifest"
    if ($DryRun) {
        Write-Host "[dry-run] Write $RuntimeRoot\manifest.json (no API keys)"
    }
    else {
        $gatewayInstalled = Join-Path $GatewayDir "qwen3-asr-gateway.exe"
        $ffmpegInstalled = Join-Path $FFmpegDir "ffmpeg.exe"
        $ffprobeInstalled = Join-Path $FFmpegDir "ffprobe.exe"
        $files = [ordered]@{
            llama_server = Get-FileRecord $llamaServer
            gateway = Get-FileRecord $gatewayInstalled
            model = Get-FileRecord $modelPath
            mmproj = Get-FileRecord $mmprojPath
            ffmpeg = Get-FileRecord $ffmpegInstalled
            ffprobe = Get-FileRecord $ffprobeInstalled
        }
        foreach ($entry in $files.GetEnumerator()) {
            if ($null -eq $entry.Value) {
                throw "Runtime file is missing: $($entry.Key)"
            }
        }

        $manifest = [ordered]@{
            schema_version = 1
            installed_at_utc = [DateTime]::UtcNow.ToString("o")
            backend = $Backend
            architecture = "win-x64"
            llama_cpp_revision = $LlamaRevision
            gateway_revision = $GatewayRevision
            model = [ordered]@{
                repository = $ModelRepository
                quantization = "Q8_0"
            }
            files = $files
            security = [ordered]@{
                contains_credentials = $false
                note = "API keys are generated or stored by the application, never by this installer."
            }
        }
        $manifestJson = $manifest | ConvertTo-Json -Depth 8
        $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText(
            (Join-Path $RuntimeRoot "manifest.json"),
            $manifestJson,
            $utf8WithoutBom
        )
    }
}
finally {
    if (-not $DryRun -and (Test-Path -LiteralPath $StagingRoot)) {
        Assert-NoReparsePoint -Path $StagingRoot
        $resolvedStaging = [System.IO.Path]::GetFullPath($StagingRoot)
        $resolvedRuntime = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd("\") + "\"
        if (-not $resolvedStaging.StartsWith($resolvedRuntime, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove staging directory outside the runtime root: $resolvedStaging"
        }
        Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
    }
}

Write-Host ""
if ($DryRun) {
    Write-Host "Dry run completed; no files were changed." -ForegroundColor Green
}
else {
    Write-Host "HearFlow engine is ready." -ForegroundColor Green
    Write-Host "Runtime: $RuntimeRoot"
    Write-Host "Backend: $Backend"
    Write-Host "Manifest: $(Join-Path $RuntimeRoot 'manifest.json')"
}
