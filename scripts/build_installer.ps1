<#
.SYNOPSIS
    Build the OpenWhisper Windows installer.

.DESCRIPTION
    Freezes the app with PyInstaller (onedir), then packs it into a single
    setup executable with Inno Setup. Prints a SHA-256 of the result for
    publishing alongside the download link.

    Prerequisites:
        .\venv\Scripts\activate
        pip install -r requirements.txt -r requirements-build.txt
        winget install -e --id JRSoftware.InnoSetup

    Code signing is optional and off by default. To sign, set both:
        $env:OPENWHISPER_SIGN_PFX  = 'C:\path\to\cert.pfx'
        $env:OPENWHISPER_SIGN_PASS = '<password>'

.PARAMETER SkipInstaller
    Freeze only; do not run Inno Setup.

.PARAMETER Clean
    Delete build\ and dist\ before starting.

.EXAMPLE
    .\scripts\build_installer.ps1 -Clean
#>
[CmdletBinding()]
param(
    [switch]$SkipInstaller,
    [switch]$Clean
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
$Python = Join-Path $RepoRoot 'venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    throw "Virtual environment not found at $Python. Create it with: python -m venv venv"
}

$Version = (& $Python -c "import _version; print(_version.__version__)").Trim()
if (-not $Version) { throw "Could not read the version from _version.py" }
Write-Step "Building OpenWhisper $Version"

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

# Freeze
Write-Step "Freezing with PyInstaller (this takes a few minutes)"
Invoke-Native $Python `
    @('-m', 'PyInstaller', '--noconfirm', '--clean', '--log-level', 'WARN', 'OpenWhisper.spec') `
    -ErrorMessage "PyInstaller failed"

$DistDir = Join-Path $RepoRoot 'dist\OpenWhisper'
$ExePath = Join-Path $DistDir 'OpenWhisper.exe'
if (-not (Test-Path $ExePath)) { throw "Expected executable not found at $ExePath" }

Write-Step "Verifying the bundle"
$DistSize = Get-TreeSize $DistDir
Write-Host "    dist\OpenWhisper: $(Format-Size $DistSize)"

# These must never appear: they would add gigabytes and are excluded in the
# spec. A stale venv or a new transitive dependency can silently reintroduce
# them, so fail the build rather than shipping a 2.5 GB installer.
$Forbidden = @('torch', 'nvidia', 'scipy', 'sympy', 'networkx')
$Internal = Join-Path $DistDir '_internal'
$Leaked = @()
foreach ($name in $Forbidden) {
    if (Test-Path (Join-Path $Internal $name)) { $Leaked += $name }
}
if ($Leaked.Count -gt 0) {
    throw "Excluded packages leaked into the bundle: $($Leaked -join ', '). Check the excludes list in OpenWhisper.spec."
}
Write-Host "    no excluded packages present" -ForegroundColor Green

# Assets the app resolves through config.bundle_root() at runtime.
$RequiredAssets = @(
    '_internal\ui_qt\styles\theme.qss',
    '_internal\ui_qt\assets\openwhisper.ico',
    '_internal\ui_qt\assets\check.svg',
    '_internal\_sounddevice_data\portaudio-binaries\libportaudio64bit.dll',
    '_internal\ctranslate2\ctranslate2.dll',
    '_internal\onnxruntime\capi\onnxruntime.dll',
    '_internal\PyQt6\Qt6\plugins\platforms\qwindows.dll',
    '_internal\PyQt6\Qt6\bin\Qt6Svg.dll',
    # Qt 6.11 imports icuuc.dll; PyQt6 6.11 wheels omit it. The spec copies
    # the Windows system ICU next to Qt6Core when the wheel has none.
    '_internal\PyQt6\Qt6\bin\icuuc.dll'
)
foreach ($asset in $RequiredAssets) {
    if (-not (Test-Path (Join-Path $DistDir $asset))) {
        throw "Required asset missing from the bundle: $asset"
    }
}

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
Write-Host "    all required assets and native libraries bundled" -ForegroundColor Green

# Optional code signing
$SignPfx = $env:OPENWHISPER_SIGN_PFX
if ($SignPfx -and (Test-Path $SignPfx)) {
    Write-Step "Signing the executable"
    $SignTool = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if (-not $SignTool) {
        throw "OPENWHISPER_SIGN_PFX is set but signtool.exe is not on PATH (install the Windows SDK)"
    }
    Invoke-Native $SignTool.Source @(
        'sign', '/fd', 'SHA256', '/f', $SignPfx, '/p', $env:OPENWHISPER_SIGN_PASS,
        '/tr', 'http://timestamp.digicert.com', '/td', 'SHA256', $ExePath
    ) -ErrorMessage "Signing the executable failed"
} else {
    Write-Host ""
    Write-Host "    Not signed. Windows SmartScreen will warn users on first run." -ForegroundColor Yellow
    Write-Host "    Set OPENWHISPER_SIGN_PFX and OPENWHISPER_SIGN_PASS to sign." -ForegroundColor Yellow
}

if ($SkipInstaller) {
    Write-Step "Done (installer skipped)"
    Write-Host "    Frozen app: $DistDir"
    exit 0
}

# Inno Setup
Write-Step "Building the installer with Inno Setup"
$Iscc = $null
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

Invoke-Native $Iscc @(
    "/DAppVersion=$Version",
    (Join-Path $RepoRoot 'installer\OpenWhisper.iss')
) -ErrorMessage "Inno Setup failed"

$SetupPath = Join-Path $RepoRoot "installer\Output\OpenWhisper-Setup-$Version.exe"
if (-not (Test-Path $SetupPath)) { throw "Installer not found at $SetupPath" }

if ($SignPfx -and (Test-Path $SignPfx)) {
    Write-Step "Signing the installer"
    Invoke-Native (Get-Command signtool.exe).Source @(
        'sign', '/fd', 'SHA256', '/f', $SignPfx, '/p', $env:OPENWHISPER_SIGN_PASS,
        '/tr', 'http://timestamp.digicert.com', '/td', 'SHA256', $SetupPath
    ) -ErrorMessage "Signing the installer failed"
}

# Report
$SetupSize = (Get-Item $SetupPath).Length
$Hash = (Get-FileHash -Algorithm SHA256 $SetupPath).Hash.ToLower()

Write-Step "Build complete"
Write-Host ""
Write-Host "  Installer : $SetupPath"
Write-Host "  Version   : $Version"
Write-Host "  Size      : $(Format-Size $SetupSize)  (unpacked $(Format-Size $DistSize))"
Write-Host "  SHA-256   : $Hash"
Write-Host ""
Write-Host "  Publish the SHA-256 next to the download link so users can verify it."
Write-Host ""
