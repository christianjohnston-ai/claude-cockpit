#Requires -Version 5.1
<#
.SYNOPSIS
Install the experimental native Windows tools for the current user.
.DESCRIPTION
Creates a private Python virtual environment and command shims. Never signs in,
reads credentials, changes execution policy/PATH, or registers a scheduled task.
Use -Update to replace differing tool files (with backups), or -CheckOnly to
validate prerequisites and inspect the plan without writing or downloading.
#>
[CmdletBinding()]
param(
    [switch]$Update,
    [switch]$CheckOnly,
    [switch]$SkipDependencies,
    [string]$Python
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'install.ps1 requires native Windows.' }
$Source = $PSScriptRoot
$ConfigSource = Join-Path $Source 'config\autoswitch.example.json'
if (-not (Test-Path -LiteralPath $ConfigSource -PathType Leaf)) { throw 'Incomplete checkout: config\autoswitch.example.json is missing.' }
$PythonArgs = @()
if ($Python) {
    $Python = (Get-Item -LiteralPath $Python -ErrorAction Stop).FullName
} else {
    $Command = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($Command) { $Python = $Command.Source; $PythonArgs = @('-3') }
    else {
        $Command = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue
        if (-not $Command) { throw 'Install Python 3.9+ from python.org, then retry (or use -Python C:\path\python.exe).' }
        $Python = $Command.Source
    }
}
& $Python @PythonArgs -c "import sys; sys.exit(0 if sys.platform == 'win32' and sys.version_info >= (3,9) else 1)"
if ($LASTEXITCODE -ne 0) { throw 'A working native Windows Python 3.9+ is required.' }
# ASCII JSON avoids PowerShell 5.1/native-codepage disagreement in Unicode homes.
$UserHomeJson = (& $Python @PythonArgs -c "import json, os; print(json.dumps(os.path.expanduser('~')))")
$UserHome = $UserHomeJson | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $UserHome) { throw 'Could not determine the Python user home.' }
$Destination = Join-Path $UserHome '.claude-profiles'
$Venv = Join-Path $Destination '.venv'
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
$Bin = Join-Path $Destination 'bin'
$ToolNames = @(
    'claude-windows.py', 'claude-usage.py',
    'claude-cockpit.py', 'claude-switch.py', 'claude-swap.py',
    'claude-autoswitch.py', 'claude-meters.py', 'claude-ping.py'
)
# Explicit inventory: never copy profile folders or credentials from the checkout.
$Files = @{}
foreach ($Name in $ToolNames) {
    $Path = Join-Path $Source "tools\$Name"
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Incomplete checkout: tools\$Name is missing." }
    $Files[$Name] = [System.IO.File]::ReadAllBytes($Path)
}
$Files['requirements-windows.txt'] = [System.IO.File]::ReadAllBytes((Join-Path $Source 'requirements-windows.txt'))
$Commands = @{
    'claude-as' = '"claude-windows.py" as'
    'claude-new-profile' = '"claude-windows.py" new-profile'
    'claude-login' = '"claude-windows.py" login'
    'claude-profiles' = '"claude-windows.py" profiles'
    'claude-verify' = '"claude-windows.py" verify'
    'claude-usage' = '"claude-usage.py"'
    'claude-cockpit' = '"claude-cockpit.py"'
    'claude-switch' = '"claude-switch.py"'
    'claude-swap' = '"claude-swap.py"'
    'claude-autoswitch' = '"claude-autoswitch.py"'
    'claude-meters' = '"claude-meters.py"'
    'claude-ping' = '"claude-ping.py"'
}
foreach ($Name in $Commands.Keys) {
    # Relative quoted paths work when the home or checkout contains spaces. Do not
    # CALL another batch file or use delayed expansion, which reinterprets argv.
    $Target = $Commands[$Name].Replace('"claude-', '"%~dp0..\claude-')
    $Shim = "@echo off`r`nsetlocal DisableDelayedExpansion`r`nset PYTHONUTF8=1`r`n`"%~dp0..\.venv\Scripts\python.exe`" $Target %*`r`nexit /b %errorlevel%`r`n"
    $Files["bin\$Name.cmd"] = [System.Text.Encoding]::ASCII.GetBytes($Shim)
}
$Changed = @()
foreach ($Relative in $Files.Keys) {
    $Target = Join-Path $Destination $Relative
    if (Test-Path -LiteralPath $Target) {
        if (-not (Test-Path -LiteralPath $Target -PathType Leaf)) { throw "Expected a file: $Target" }
        $Existing = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($Target))
        if ($Existing -ne [Convert]::ToBase64String($Files[$Relative])) { $Changed += $Relative }
    }
}
if ($Changed.Count -gt 0 -and -not $Update) {
    throw ("Existing tools differ: {0}. Nothing changed. Review and rerun with -Update to back them up and replace them." -f ($Changed -join ', '))
}
Write-Host "Install location: $Destination"
Write-Host 'Dependency: windows-curses 2.4.2 from https://pypi.org/simple (binary wheel only).'
Write-Host 'No PATH, execution policy, credentials, existing sessions, or startup jobs are changed.'
if ($CheckOnly) { Write-Host 'Check complete. Nothing was written or downloaded.'; return }
# Complete dependency setup before publishing/replacing the tool files. A failed
# download may leave the venv, but never destroys profiles or replaces old tools.
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    if (Test-Path -LiteralPath $Venv) { throw "Incomplete environment at $Venv. Inspect it before removing/recreating it." }
    & $Python @PythonArgs -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw 'Creating the virtual environment failed. No tools were replaced.' }
}
if (-not $SkipDependencies) {
    & $VenvPython -c "import importlib.metadata as m, sys; sys.exit(0 if any(d.metadata['Name'].lower().replace('_', '-') == 'windows-curses' and d.version == '2.4.2' for d in m.distributions()) else 1)"
    if ($LASTEXITCODE -ne 0) {
        # Ignore machine/user pip config as well as PIP_* index overrides. In
        # particular, do not inherit an extra-index-url from another project.
        $PreviousPipConfig = [Environment]::GetEnvironmentVariable('PIP_CONFIG_FILE', 'Process')
        try {
            $env:PIP_CONFIG_FILE = 'nul'
            & $VenvPython -m pip --isolated install --disable-pip-version-check --no-input --only-binary=:all: --no-deps --index-url https://pypi.org/simple -r (Join-Path $Source 'requirements-windows.txt')
            if ($LASTEXITCODE -ne 0) { throw 'windows-curses installation failed. No tool files were replaced. Check Python architecture/version and PyPI access.' }
        } finally {
            [Environment]::SetEnvironmentVariable('PIP_CONFIG_FILE', $PreviousPipConfig, 'Process')
        }
    }
    & $VenvPython -c 'import curses'
    if ($LASTEXITCODE -ne 0) { throw 'The virtual environment cannot import curses. No tools were replaced.' }
} else {
    Write-Warning 'Dependency installation skipped explicitly; the TUI needs windows-curses before it can run.'
}
if ($Changed.Count -gt 0) {
    $Stamp = Get-Date -Format 'yyyyMMdd_HHmmss_fffffff'
    $Backup = Join-Path $UserHome ".claude-profiles-backups\$Stamp"
    foreach ($Relative in $Changed) {
        $BackupFile = Join-Path $Backup $Relative
        [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($BackupFile)) | Out-Null
        Copy-Item -LiteralPath (Join-Path $Destination $Relative) -Destination $BackupFile
    }
    Write-Host "Replaced-file backups: $Backup"
}
foreach ($Relative in $Files.Keys) {
    $Target = Join-Path $Destination $Relative
    if (-not (Test-Path -LiteralPath $Target) -or $Changed -contains $Relative) {
        [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($Target)) | Out-Null
        [System.IO.File]::WriteAllBytes($Target, $Files[$Relative])
    }
}
$Config = Join-Path $Destination 'autoswitch.json'
if (-not (Test-Path -LiteralPath $Config)) {
    Copy-Item -LiteralPath $ConfigSource -Destination $Config
}
Write-Host ''
Write-Host 'Installed. Add the commands to this PowerShell session only:'
Write-Host ('  $env:PATH = "{0};$env:PATH"' -f $Bin)
Write-Host 'Then: claude-new-profile personal; claude-login personal; claude-cockpit'
Write-Host 'Read docs/windows.md for limitations and manual Windows validation.'
