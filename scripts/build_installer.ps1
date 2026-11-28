<#
.SYNOPSIS
    Build the OpenWhisper Windows installer(s).

.DESCRIPTION
    Freezes the app with PyInstaller (onedir), then packs it into setup
    executable(s) with Inno Setup and native update archive(s). Prints SHA-256
    digests of the results for publishing alongside the download links.

    Prerequisites:
        .\venv\Scripts\activate
        pip install -r requirements.txt -r requirements-build.txt -c requirements-release-constraints.txt
        winget install -e --id JRSoftware.InnoSetup

    Code signing is optional and off by default. To sign, configure exactly
    one method (see scripts\windows_signing.py for details):
        $env:OPENWHISPER_SIGN_CERT_SHA1 = '<thumbprint of a cert in the store>'
    or
        $env:OPENWHISPER_SIGN_PFX  = 'C:\path\to\cert.pfx'
        $env:OPENWHISPER_SIGN_PASS = '<password>'
    or, for Azure Trusted Signing,
        $env:OPENWHISPER_SIGN_DLIB     = '<path>\Azure.CodeSigning.Dlib.dll'
        $env:OPENWHISPER_SIGN_METADATA = '<path>\metadata.json'
    The app executables, the uninstaller and the setup exes are then signed
    and timestamped with SignTool. A partial configuration fails the build.

.PARAMETER SkipInstaller
    Freeze only; do not run Inno Setup.

.PARAMETER Clean
    Delete build\ and dist\ before starting.

.PARAMETER Edition
    Edition to build: 'full' (default) or 'client' (lightweight Client-Only
    edition omitting local ML/FFmpeg runtimes and bundled transcribe.cpp).

.PARAMETER IncludeClient
    When building the 'full' edition, also build, verify, sign, and package
    the Client-Only Windows artifacts in the same run.

.EXAMPLE
    .\scripts\build_installer.ps1 -Clean
    .\scripts\build_installer.ps1 -Clean -Edition client
    .\scripts\build_installer.ps1 -Clean -IncludeClient
#>
[CmdletBinding()]
param(
    [switch]$SkipInstaller,
    [switch]$Clean,
    [ValidateSet('full', 'client')][string]$Edition = 'full',
    [switch]$IncludeClient
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Write-Step($Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Native {
    <#
    .SYNOPSIS
        Run a native executable and fail on a non-zero exit code.
    .DESCRIPTION
        PyInstaller and ISCC write progress to stderr. Under
        $ErrorActionPreference = 'Stop', Windows PowerShell wraps each stderr
        line in a NativeCommandError and aborts a perfectly successful build,
        so stderr is demoted to normal output here and success is judged by
        $LASTEXITCODE, which is the only reliable signal for a native command.
    #>
    param(
        [Parameter(Mandatory)][string]$Exe,
        [string[]]$Arguments = @(),
        [Parameter(Mandatory)][string]$ErrorMessage
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$ErrorMessage (exit code $LASTEXITCODE)"
        }
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Format-Size($Bytes) {
    if ($Bytes -ge 1GB) { return "{0:N2} GB" -f ($Bytes / 1GB) }
    if ($Bytes -ge 1MB) { return "{0:N1} MB" -f ($Bytes / 1MB) }
    return "{0:N0} KB" -f ($Bytes / 1KB)
}

function Get-TreeSize($Path) {
    $items = Get-ChildItem -Path $Path -Recurse -File -ErrorAction SilentlyContinue
    if (-not $items) { return 0 }
    return ($items | Measure-Object -Property Length -Sum).Sum
}

# Locate the interpreter
# OPENWHISPER_PYTHON, then venv\ (pip), then .venv\ (uv sync), as the
# Linux and macOS builders do.
$Python = $env:OPENWHISPER_PYTHON
if (-not $Python) {
    $Python = @('venv', '.venv') |
        ForEach-Object { Join-Path $RepoRoot "$_\Scripts\python.exe" } |
        Where-Object { Test-Path $_ } |
        Select-Object -First 1
}
if (-not $Python -or -not (Test-Path $Python)) {
    throw "Virtual environment not found. Create one with: python -m venv venv (or uv sync --locked)"
}

$Version = (& $Python -c "import _version; print(_version.__version__)").Trim()
if (-not $Version) { throw "Could not read the version from _version.py" }

$EditionsToBuild = if ($Edition -eq 'client') {
    @('client')
} elseif ($IncludeClient) {
    # Build Client first, then Full so dist\OpenWhisper ends on the Full bundle.
    @('client', 'full')
} else {
    @('full')
}

Write-Step "Building OpenWhisper $Version (editions: $($EditionsToBuild -join ', '))"

Invoke-Native $Python @('-c', 'import PyInstaller') `
    -ErrorMessage "PyInstaller is missing. Run: pip install -r requirements-build.txt"

# Clean
if ($Clean) {
    Write-Step "Cleaning previous build output"
    foreach ($dir in @('build', 'dist', 'installer\Output')) {
        $full = Join-Path $RepoRoot $dir
        if (Test-Path $full) {
            Remove-Item -Recurse -Force $full
            Write-Host "    removed $dir"
        }
    }
}

# Icon
Write-Step "Generating application icon"
Invoke-Native $Python @('scripts\generate_icon.py') -ErrorMessage "Icon generation failed"

# Updater helper (onefile, stdlib-only). Copied into the app onedir so first
# install and later native updates have a commit process outside {app}.
Write-Step "Freezing the updater helper"
Invoke-Native $Python `
    @('-m', 'PyInstaller', '--noconfirm', '--clean', '--log-level', 'WARN', 'OpenWhisperUpdater.spec') `
    -ErrorMessage "Updater helper freeze failed"
$HelperExe = Join-Path $RepoRoot 'dist\OpenWhisperUpdater.exe'
if (-not (Test-Path $HelperExe)) { throw "Expected updater helper not found at $HelperExe" }

# Empty when unsigned; otherwise Inno signs the uninstaller and setup exe.
$InnoSignArgs = @(& $Python scripts\windows_signing.py inno-args)
if ($LASTEXITCODE -ne 0) { throw "Could not prepare installer signing" }

$Iscc = $null
if (-not $SkipInstaller) {
    $Candidates = @(
        # winget's default --scope user location, listed first: a per-user install
        # needs no admin rights, matching how this project ships.
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $Candidates) {
        if (Test-Path $candidate) { $Iscc = $candidate; break }
    }
    if (-not $Iscc) {
        $found = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($found) { $Iscc = $found.Source }
    }
    if (-not $Iscc) {
        throw "Inno Setup not found. Install it with: winget install -e --id JRSoftware.InnoSetup"
    }
}

$OutputDir = Join-Path $RepoRoot 'installer\Output'
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$SetupOnlyWindows = (& $Python -c "from services.update_contract import is_setup_bridge_release; print(is_setup_bridge_release('$Version'))").Trim() -eq 'True'

$PreviousEditionEnv = $env:OPENWHISPER_EDITION
$BuiltArtifacts = @{}
$ClientSetupPath = $null

try {
    foreach ($CurrentEdition in $EditionsToBuild) {
        $IsClient = ($CurrentEdition -eq 'client')
        $env:OPENWHISPER_EDITION = $CurrentEdition

        # Freeze
        Write-Step "Freezing OpenWhisper ($CurrentEdition) with PyInstaller (this takes a few minutes)"
        Invoke-Native $Python `
            @('-m', 'PyInstaller', '--noconfirm', '--clean', '--log-level', 'WARN', 'OpenWhisper.spec') `
            -ErrorMessage "PyInstaller failed ($CurrentEdition)"

        $DistDir = Join-Path $RepoRoot 'dist\OpenWhisper'
        $ExePath = Join-Path $DistDir 'OpenWhisper.exe'
        if (-not (Test-Path $ExePath)) { throw "Expected executable not found at $ExePath" }
        Copy-Item -Force $HelperExe (Join-Path $DistDir 'OpenWhisperUpdater.exe')
        Write-Host "    copied OpenWhisperUpdater.exe into the app bundle"

        if (-not $IsClient) {
            Write-Step "Staging bundled Parakeet Unified runtime (transcribe.cpp)"
            Invoke-Native $Python `
                @('scripts\stage_bundled_runtime.py', '--dest', (Join-Path $DistDir '_internal')) `
                -ErrorMessage "Bundled speech runtime staging failed"
        }

        Write-Step "Verifying the $CurrentEdition bundle"
        $DistSize = Get-TreeSize $DistDir
        Write-Host "    dist\OpenWhisper ($CurrentEdition): $(Format-Size $DistSize)"

        # These must never appear: they would add gigabytes and are excluded in the
        # spec. A stale venv or a new transitive dependency can silently reintroduce
        # them, so fail the build rather than shipping a 2.5 GB installer.
        $Forbidden = @('torch', 'nvidia', 'scipy', 'sympy', 'networkx')
        if ($IsClient) {
            $Forbidden += @(
                'ctranslate2', 'faster_whisper', 'onnxruntime', 'tokenizers',
                'huggingface_hub', 'hf_xet', 'av', 'av.libs',
                'bundled_components\asr-transcribe'
            )
        }
        $Internal = Join-Path $DistDir '_internal'
        $Leaked = @()
        foreach ($name in $Forbidden) {
            if (Test-Path (Join-Path $Internal $name)) { $Leaked += $name }
        }
        if ($IsClient) {
            foreach ($pattern in @('ctranslate2*.dll', 'onnxruntime*.dll', 'avcodec-*.dll', 'avformat-*.dll', 'avutil-*.dll', 'swresample-*.dll', 'swscale-*.dll')) {
                $hits = Get-ChildItem -Path $Internal -Recurse -Filter $pattern -ErrorAction SilentlyContinue
                if ($hits) { $Leaked += $pattern }
            }
        }
        if ($Leaked.Count -gt 0) {
            throw "Excluded packages/assets leaked into the $CurrentEdition bundle: $($Leaked -join ', '). Check the excludes list in OpenWhisper.spec."
        }
        Write-Host "    no excluded packages present" -ForegroundColor Green

        # Assets the app resolves through config.bundle_root() at runtime.
        $RequiredAssets = @(
            '_internal\edition.json',
            '_internal\services\whisper_models.json',
            '_internal\services\local_asr\models.json',
            '_internal\ui_qt\styles\theme.qss',
            '_internal\ui_qt\assets\openwhisper.ico',
            '_internal\ui_qt\assets\check.svg',
            '_internal\THIRD_PARTY_NOTICES.md',
            '_internal\third_party_licenses\PyQt6\LICENSE',
            '_internal\third_party_licenses\Qt\LICENSE',
            '_internal\_sounddevice_data\portaudio-binaries\libportaudio64bit.dll',
            '_internal\PyQt6\Qt6\plugins\platforms\qwindows.dll',
            '_internal\PyQt6\Qt6\bin\Qt6Svg.dll',
            # Qt 6.11 imports icuuc.dll; PyQt6 6.11 wheels omit it. The spec copies
            # the Windows system ICU next to Qt6Core when the wheel has none.
            '_internal\PyQt6\Qt6\bin\icuuc.dll',
            'OpenWhisperUpdater.exe'
        )
        if (-not $IsClient) {
            $RequiredAssets += @(
                '_internal\ctranslate2\ctranslate2.dll',
                '_internal\onnxruntime\capi\onnxruntime.dll',
                '_internal\bundled_components\asr-transcribe\.installed',
                '_internal\bundled_components\asr-transcribe\manifest.json',
                '_internal\bundled_components\asr-transcribe\python.exe',
                '_internal\bundled_components\asr-transcribe\transcribe-native\transcribe.dll',
                '_internal\bundled_components\asr-transcribe\transcribe-native\ggml-vulkan.dll'
            )
        }
        foreach ($asset in $RequiredAssets) {
            if (-not (Test-Path (Join-Path $DistDir $asset))) {
                throw "Required asset missing from the $CurrentEdition bundle: $asset"
            }
        }

        $EditionManifest = Get-Content -Raw -LiteralPath (Join-Path $Internal 'edition.json') | ConvertFrom-Json
        if ($EditionManifest.edition -ne $CurrentEdition) {
            throw "edition.json reported '$($EditionManifest.edition)', expected '$CurrentEdition'"
        }

        if (-not $IsClient) {
            # PyAV's FFmpeg build. These have delvewheel-mangled names that collide with
            # the avcodec-/avutil-/swscale- patterns used to strip Qt Multimedia's copies;
            # losing them breaks `import av` with an opaque "DLL load failed" at startup.
            $AvLibs = Join-Path $DistDir '_internal\av.libs'
            foreach ($lib in @('avcodec', 'avformat', 'avutil', 'swresample', 'swscale')) {
                $hits = Get-ChildItem -Path $AvLibs -Filter "$lib-*.dll" -ErrorAction SilentlyContinue
                if (-not $hits) {
                    throw "PyAV FFmpeg library '$lib' is missing from av.libs. The Qt exclusion filter in OpenWhisper.spec is matching too broadly."
                }
            }
        }
        Write-Host "    all required assets and native libraries verified ($CurrentEdition)" -ForegroundColor Green

        # Exercise the same import/Qt bootstrap that the Linux package gate runs. This
        # must happen before signing or archiving so a broken frozen tree cannot ship.
        Invoke-Native $ExePath @('--self-test') `
            -ErrorMessage "Frozen application self-test failed ($CurrentEdition)"
        Write-Host "    frozen application self-test passed ($CurrentEdition)" -ForegroundColor Green
        $HealthOutput = if ($IsClient) { 'build/release-health-client.json' } else { 'build/release-health.json' }
        Invoke-Native $Python @('scripts/check_release_health.py', '--executable', $ExePath,
            '--output', $HealthOutput, '--timeout', '45') `
            -ErrorMessage "Frozen lifecycle/performance gate failed ($CurrentEdition)"

        # Optional code signing. Before the archive and the installer are packed,
        # so both carry the signed executables (and the archive manifest their hashes).
        $HelperInBundle = Join-Path $DistDir 'OpenWhisperUpdater.exe'
        Write-Step "Code signing the $CurrentEdition executables"
        Invoke-Native $Python @('scripts\windows_signing.py', 'sign', $ExePath, $HelperInBundle) `
            -ErrorMessage "Signing the $CurrentEdition executables failed"

        # Native update archive from the identical, optionally signed dist tree.
        Write-Step "Packing the $CurrentEdition native update archive"
        $ArchiveName = if ($IsClient) { "OpenWhisper-Client-$Version-win64.tar.xz" } else { "OpenWhisper-$Version-win64.tar.xz" }
        $ArchiveDir = $OutputDir
        if ($SetupOnlyWindows) {
            $ArchiveDir = Join-Path $RepoRoot '.tmp\update-validation'
            New-Item -ItemType Directory -Force -Path $ArchiveDir | Out-Null
            $StaleBridgeArchive = Join-Path $OutputDir $ArchiveName
            if (Test-Path -LiteralPath $StaleBridgeArchive) {
                Move-Item -LiteralPath $StaleBridgeArchive -Destination (Join-Path $ArchiveDir "previous-$ArchiveName") -Force
            }
        }
        $ArchivePath = Join-Path $ArchiveDir $ArchiveName
        if (Test-Path $ArchivePath) { Remove-Item -Force $ArchivePath }
        Invoke-Native $Python @(
            'scripts\pack_update_archive.py',
            '--dist', $DistDir,
            '--output', $ArchivePath,
            '--version', $Version,
            '--edition', $CurrentEdition
        ) -ErrorMessage "Native update archive failed ($CurrentEdition)"

        if ($SkipInstaller) {
            $BuiltArtifacts[$CurrentEdition] = @{
                DistDir = $DistDir
                DistSize = $DistSize
                ArchivePath = $ArchivePath
            }
            continue
        }

        if ($IsClient) {
            Write-Step "Building the Client-Only installer with Inno Setup"
            Invoke-Native $Iscc @(
                "/DAppVersion=$Version"
                "/DOutputPrefix=OpenWhisper-Client"
                $InnoSignArgs
                (Join-Path $RepoRoot 'installer\OpenWhisper.iss')
            ) -ErrorMessage "Inno Setup (client) failed"

            $ClientSetupPath = Join-Path $RepoRoot "installer\Output\OpenWhisper-Client-Setup-$Version.exe"
            if (-not (Test-Path $ClientSetupPath)) { throw "Client installer not found at $ClientSetupPath" }

            $ClientSignReport = if ($EditionsToBuild.Count -gt 1) { 'build\windows-signing-client.json' } else { 'build\windows-signing.json' }
            Write-Step "Verifying Client-Only code signatures"
            Invoke-Native $Python @(
                'scripts\windows_signing.py', 'verify', '--output', $ClientSignReport,
                $ExePath, $HelperInBundle, $ClientSetupPath
            ) -ErrorMessage "Client-Only code signature verification failed"

            $BuiltArtifacts['client'] = @{
                DistSize = $DistSize
                SetupPath = $ClientSetupPath
                ArchivePath = $ArchivePath
            }
        } else {
            # Inno Setup (Full + All-in-One)
            Write-Step "Building the installer with Inno Setup"
            Invoke-Native $Iscc @(
                "/DAppVersion=$Version"
                $InnoSignArgs
                (Join-Path $RepoRoot 'installer\OpenWhisper.iss')
            ) -ErrorMessage "Inno Setup failed"

            $SetupPath = Join-Path $RepoRoot "installer\Output\OpenWhisper-Setup-$Version.exe"
            if (-not (Test-Path $SetupPath)) { throw "Installer not found at $SetupPath" }

            # Stage the compact English speech model into a separate overlay directory so
            # dist\OpenWhisper and the slim native update archive stay unchanged while the
            # all-in-one setup exe includes everything needed to dictate offline out of the box.
            $BundledModelStage = Join-Path $RepoRoot "build\all-in-one-models"
            Write-Step "Staging bundled speech model for all-in-one installer"
            Invoke-Native $Python @(
                'scripts\stage_bundled_model.py',
                '--dest', $BundledModelStage
            ) -ErrorMessage "Failed to stage bundled speech model"

            Write-Step "Building all-in-one Windows installer"
            Invoke-Native $Iscc @(
                "/DAppVersion=$Version"
                "/DOutputPrefix=OpenWhisper-AllInOne"
                "/DBundledModelDir=$BundledModelStage"
                $InnoSignArgs
                (Join-Path $RepoRoot 'installer\OpenWhisper.iss')
            ) -ErrorMessage "Inno Setup (all-in-one) failed"

            $AllInOneSetupPath = Join-Path $RepoRoot "installer\Output\OpenWhisper-AllInOne-Setup-$Version.exe"
            if (-not (Test-Path $AllInOneSetupPath)) { throw "All-in-one installer not found at $AllInOneSetupPath" }

            # Every signature must verify, chain to a trusted root and carry a
            # timestamp; an unsigned build must not carry a stray signature either.
            Write-Step "Verifying code signatures"
            $VerifyTargets = @($ExePath, $HelperInBundle, $SetupPath, $AllInOneSetupPath)
            if ($ClientSetupPath) {
                $VerifyTargets += $ClientSetupPath
            }
            Invoke-Native $Python (
                @('scripts\windows_signing.py', 'verify', '--output', 'build\windows-signing.json') +
                $VerifyTargets
            ) -ErrorMessage "Code signature verification failed"

            $BuiltArtifacts['full'] = @{
                DistSize = $DistSize
                SetupPath = $SetupPath
                AllInOneSetupPath = $AllInOneSetupPath
                ArchivePath = $ArchivePath
            }
        }
    }
} finally {
    if ($null -eq $PreviousEditionEnv) {
        Remove-Item Env:OPENWHISPER_EDITION -ErrorAction SilentlyContinue
    } else {
        $env:OPENWHISPER_EDITION = $PreviousEditionEnv
    }
}

if ($SkipInstaller) {
    Write-Step "Done (installer skipped)"
    foreach ($ed in $EditionsToBuild) {
        $info = $BuiltArtifacts[$ed]
        $ArchiveHash = (Get-FileHash -Algorithm SHA256 $info.ArchivePath).Hash.ToLower()
        Write-Host "    [$ed] Archive : $($info.ArchivePath)"
        Write-Host "    [$ed] SHA-256 : $ArchiveHash"
    }
    exit 0
}

# Report
Write-Step "Build complete"
Write-Host ""
Write-Host "  Version    : $Version"
if ($BuiltArtifacts.ContainsKey('full')) {
    $full = $BuiltArtifacts['full']
    $SetupSize = (Get-Item $full.SetupPath).Length
    $Hash = (Get-FileHash -Algorithm SHA256 $full.SetupPath).Hash.ToLower()
    $AllInOneSize = (Get-Item $full.AllInOneSetupPath).Length
    $AllInOneHash = (Get-FileHash -Algorithm SHA256 $full.AllInOneSetupPath).Hash.ToLower()
    $ArchiveSize = (Get-Item $full.ArchivePath).Length
    $ArchiveHash = (Get-FileHash -Algorithm SHA256 $full.ArchivePath).Hash.ToLower()
    Write-Host "  Installer  : $($full.SetupPath)"
    Write-Host "  All-in-One : $($full.AllInOneSetupPath)"
    Write-Host "  Archive    : $($full.ArchivePath)"
    Write-Host "  Setup      : $(Format-Size $SetupSize)  (unpacked $(Format-Size $full.DistSize))"
    Write-Host "  All-in-One : $(Format-Size $AllInOneSize)"
    Write-Host "  Archive    : $(Format-Size $ArchiveSize)"
    Write-Host "  Setup SHA-256      : $Hash"
    Write-Host "  All-in-One SHA-256 : $AllInOneHash"
    Write-Host "  Archive SHA-256    : $ArchiveHash"
}
if ($BuiltArtifacts.ContainsKey('client')) {
    $client = $BuiltArtifacts['client']
    $ClientSetupSize = (Get-Item $client.SetupPath).Length
    $ClientHash = (Get-FileHash -Algorithm SHA256 $client.SetupPath).Hash.ToLower()
    $ClientArchiveSize = (Get-Item $client.ArchivePath).Length
    $ClientArchiveHash = (Get-FileHash -Algorithm SHA256 $client.ArchivePath).Hash.ToLower()
    Write-Host "  Client     : $($client.SetupPath)"
    Write-Host "  Client Arc : $($client.ArchivePath)"
    Write-Host "  Client     : $(Format-Size $ClientSetupSize)  (unpacked $(Format-Size $client.DistSize))"
    Write-Host "  Client Arc : $(Format-Size $ClientArchiveSize)"
    Write-Host "  Client SHA-256     : $ClientHash"
    Write-Host "  Client Arc SHA-256 : $ClientArchiveHash"
}
Write-Host ""
if ($SetupOnlyWindows) {
    Write-Host "  This is a required setup-only bridge release."
    Write-Host "  Publish only the setup exes from installer\Output; the archives are validation-only."
} else {
    Write-Host "  Upload all Windows artifacts to a draft release and wait for every digest field."
}
Write-Host ""
