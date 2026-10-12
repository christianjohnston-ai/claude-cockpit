#Requires -Version 5.1
# Parser and offline fixture smoke tests. No credentials, login, or network.
# Run: powershell -NoProfile -File .\tests\test_windows_installer.ps1
[CmdletBinding()]
param([string]$Python)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this test on native Windows.' }
$Repository = Split-Path -Parent $PSScriptRoot
$Installer = Join-Path $Repository 'install.ps1'
$ParseErrors = $null; $Tokens = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$Tokens, [ref]$ParseErrors)
if ($ParseErrors.Count) { throw ($ParseErrors | Out-String) }
if (-not $Python) {
    if (Get-Command py.exe -ErrorAction SilentlyContinue) { $Python = & py.exe -3 -c 'import sys; print(sys.executable)' }
    else { $Python = & python.exe -c 'import sys; print(sys.executable)' }
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Native Python was not found.' }
function Assert-True($Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
$Fixture = Join-Path ([System.IO.Path]::GetTempPath()) ('cockpit test ' + [Guid]::NewGuid().ToString('N'))
$FakeHome = Join-Path $Fixture 'user with spaces'
$Saved = @{}
foreach ($Name in @('HOME', 'USERPROFILE', 'TEMP', 'TMP', 'COCKPIT_CLAUDE_BIN', 'COCKPIT_FLEET_CONFIG_DIR', 'CLAUDE_CONFIG_DIR', 'PYTHONUTF8')) {
    $Saved[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
}
try {
    [System.IO.Directory]::CreateDirectory($FakeHome) | Out-Null
    $FakeTemp = Join-Path $Fixture 'temp'
    [System.IO.Directory]::CreateDirectory($FakeTemp) | Out-Null
    $env:TEMP = $FakeTemp; $env:TMP = $FakeTemp
    $env:HOME = $FakeHome; $env:USERPROFILE = $FakeHome; $env:PYTHONUTF8 = '1'
    [Environment]::SetEnvironmentVariable('COCKPIT_FLEET_CONFIG_DIR', $null, 'Process')
    $env:CLAUDE_CONFIG_DIR = 'sentinel-parent-config'
    $env:COCKPIT_CLAUDE_BIN = Join-Path $Fixture 'missing-fake-claude.exe'
    $InstallRoot = Join-Path $FakeHome '.claude-profiles'
    & $Installer -Python $Python -CheckOnly
    Assert-True (-not (Test-Path -LiteralPath $InstallRoot)) '-CheckOnly wrote install files.'
    & $Installer -Python $Python -SkipDependencies
    $VenvPython = Join-Path $InstallRoot '.venv\Scripts\python.exe'
    $Entry = Join-Path $InstallRoot 'claude-windows.py'
    Assert-True (Test-Path -LiteralPath $VenvPython) 'No isolated interpreter.'
    $Tool = Join-Path $InstallRoot 'claude-usage.py'
    $Original = (Get-FileHash -LiteralPath $Tool).Hash
    $Config = Join-Path $InstallRoot 'autoswitch.json'
    $ConfigHash = (Get-FileHash -LiteralPath $Config).Hash
    & $Installer -Python $Python -SkipDependencies
    Assert-True ((Get-FileHash -LiteralPath $Tool).Hash -eq $Original) 'Repeat install changed a tool.'
    Assert-True ((Get-FileHash -LiteralPath $Config).Hash -eq $ConfigHash) 'Repeat install changed settings.'
    Add-Content -LiteralPath $Tool -Value '# fixture local edit'
    $Modified = (Get-FileHash -LiteralPath $Tool).Hash
    $Refused = $false
    try { & $Installer -Python $Python -SkipDependencies } catch { $Refused = $true }
    Assert-True $Refused 'Installer silently overwrote a changed tool.'
    Assert-True ((Get-FileHash -LiteralPath $Tool).Hash -eq $Modified) 'Refusal changed a tool.'
    & $Installer -Python $Python -SkipDependencies -Update
    Assert-True ((Get-FileHash -LiteralPath $Tool).Hash -eq $Original) 'Update did not restore tool.'
    $Backups = @(Get-ChildItem -LiteralPath (Join-Path $FakeHome '.claude-profiles-backups') -Filter 'claude-usage.py' -Recurse)
    Assert-True ($Backups.Count -eq 1) 'Expected exactly one tool backup.'
    Assert-True ((Get-FileHash -LiteralPath $Backups[0].FullName).Hash -eq $Modified) 'Backup differs from replaced tool.'
    Assert-True ((Get-FileHash -LiteralPath $Config).Hash -eq $ConfigHash) 'Update changed settings.'
    & $VenvPython $Entry new-profile personal
    Assert-True ($LASTEXITCODE -eq 0) 'Fixture profile creation failed.'
    $ProfileConfig = Join-Path $InstallRoot 'personal\.claude.json'
    $FreshConfig = [System.IO.File]::ReadAllText($ProfileConfig) | ConvertFrom-Json
    Assert-True (@($FreshConfig.PSObject.Properties).Count -eq 0) 'Fresh profile inherited settings.'
    Assert-True ($env:CLAUDE_CONFIG_DIR -eq 'sentinel-parent-config') 'Parent environment was changed.'
    # Exercise generated CMD path/exit propagation without running real Claude.
    & (Join-Path $InstallRoot 'bin\claude-verify.cmd')
    Assert-True ($LASTEXITCODE -ne 0) 'Verify accepted a nonexistent fake Claude executable.'
    # Compile an offline, credential-free CLI stub when the Windows C# compiler
    # is present. No downloads and no Add-Type-generated host dependency.
    $Compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    if (-not (Test-Path -LiteralPath $Compiler)) { $Compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe' }
    if (Test-Path -LiteralPath $Compiler) {
        $StubSource = Join-Path $Fixture 'stub.cs'
        $Stub = Join-Path $Fixture 'fake claude.exe'
        [System.IO.File]::WriteAllText($StubSource, @'
using System;
public static class Stub {
    public static int Main(string[] args) {
        Console.WriteLine("CONFIG=" + Environment.GetEnvironmentVariable("CLAUDE_CONFIG_DIR"));
        foreach (string arg in args) Console.WriteLine("ARG=" + arg);
        return Array.IndexOf(args, "--exit-23") >= 0 ? 23 : 0;
    }
}
'@)
        & $Compiler /nologo /target:exe "/out:$Stub" $StubSource
        Assert-True ($LASTEXITCODE -eq 0) 'Offline stub compilation failed.'
        $env:COCKPIT_CLAUDE_BIN = $Stub
        $Output = & (Join-Path $InstallRoot 'bin\claude-as.cmd') personal 'argument with spaces' --exit-23
        Assert-True ($LASTEXITCODE -eq 23) 'CMD launcher lost the child exit code.'
        Assert-True ($Output -contains 'ARG=argument with spaces') 'CMD launcher lost an argument boundary.'
        Assert-True ($Output -contains ('CONFIG=' + (Join-Path $InstallRoot 'personal'))) 'Child profile environment is wrong.'
        Assert-True ($env:CLAUDE_CONFIG_DIR -eq 'sentinel-parent-config') 'Launcher changed parent environment.'
    } else { Write-Warning 'SKIP: C# compiler absent; native Claude stub/argv test was not run.' }
    Write-Host 'PASS: offline Windows installer fixtures (TUI, pip download and live login not tested).'
} finally {
    foreach ($Name in $Saved.Keys) { [Environment]::SetEnvironmentVariable($Name, $Saved[$Name], 'Process') }
    # Delete only the unique test tree, never a real profile or interpreter.
    if (Test-Path -LiteralPath $Fixture) { Remove-Item -LiteralPath $Fixture -Recurse -Force }
}
