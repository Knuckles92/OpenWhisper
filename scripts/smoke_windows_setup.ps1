<#
Run only on a disposable CI runner. Exercise a released install, setup upgrade,
frozen imports, stale-runtime cleanup, data preservation, and uninstall.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PreviousSetup,
    [Parameter(Mandatory)][string]$NewSetup,
    [Parameter(Mandatory)][string]$Version
)
$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or -not $env:RUNNER_TEMP) {
    throw 'This lifecycle check requires a disposable GitHub Actions runner.'
}
$UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{CA36AD0A-13B9-4737-87AD-ADB54A28EFC9}_is1'
foreach ($hive in @('HKCU', 'HKLM')) {
    if (Test-Path "$hive`:\$UninstallKey") {
        throw 'An existing installation is present; refusing to replace it.'
    }
}
$Target = Join-Path $env:RUNNER_TEMP 'OpenWhisper-setup-smoke'
$Data = Join-Path $env:LOCALAPPDATA 'OpenWhisper'
$Logs = Join-Path $env:RUNNER_TEMP 'openwhisper-setup-logs'
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
function Run-Setup([string]$Installer, [string]$Name) {
    $process = Start-Process -FilePath $Installer -ArgumentList @(
        '/SP-', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
        '/CURRENTUSER', "/DIR=`"$Target`"", "/LOG=`"$Logs\$Name.log`""
    ) -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit(300000)) {
        $process.Kill()
        throw "$Name timed out"
    }
    if ($process.ExitCode -ne 0) { throw "$Name failed: $($process.ExitCode)" }
}
Run-Setup (Resolve-Path -LiteralPath $PreviousSetup).Path 'previous'
$Stale = Join-Path $Target '_internal\retired-smoke-module.dll'
[IO.File]::WriteAllText($Stale, 'old dependency')
New-Item -ItemType Directory -Force -Path $Data | Out-Null
$Preserved = Join-Path $Data 'setup-smoke-user-data.txt'
[IO.File]::WriteAllText($Preserved, 'keep my data')
Run-Setup (Resolve-Path -LiteralPath $NewSetup).Path 'upgrade'
if (Test-Path -LiteralPath $Stale) { throw 'Setup left a retired runtime file behind.' }
if ([IO.File]::ReadAllText($Preserved) -ne 'keep my data') { throw 'Setup changed user data.' }
$Registered = Get-ItemProperty "HKCU:\$UninstallKey"
if ($Registered.DisplayVersion -ne $Version) { throw 'Installed version is stale.' }
$App = Join-Path $Target 'OpenWhisper.exe'
$Test = Start-Process -FilePath $App -ArgumentList '--self-test' -WindowStyle Hidden -PassThru
if (-not $Test.WaitForExit(120000)) {
    $Test.Kill()
    throw 'Upgraded application self-test timed out.'
}
if ($Test.ExitCode -ne 0) { throw 'Upgraded application self-test failed.' }
$Uninstaller = Join-Path $Target 'unins000.exe'
$Uninstall = Start-Process -FilePath $Uninstaller -ArgumentList @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'
) -WindowStyle Hidden -PassThru
if (-not $Uninstall.WaitForExit(120000)) {
    $Uninstall.Kill()
    throw 'Uninstall timed out.'
}
if ($Uninstall.ExitCode -ne 0) { throw 'Uninstall failed.' }
if (Test-Path -LiteralPath $App) { throw 'Uninstall left the application behind.' }
if (-not (Test-Path -LiteralPath $Preserved)) { throw 'Uninstall removed retained user data.' }
Write-Host 'Previous release -> clean setup upgrade -> self-test -> uninstall passed.'
