#requires -Version 5.1
<#
.SYNOPSIS
  Install Fusion, dependencies and the AndrewCode, Claude Code and Codex plugins.
.DESCRIPTION
  Defaults to all three hosts. Stages a slim runtime, verifies forms and MCP,
  adds the fusion command to User PATH and registers plugins through native
  host interfaces. Missing dependencies/hosts are installed where supported.
  -SkipDependencies is offline mode; missing required dependencies still fail.
  -DryRun makes no changes and runs no host commands.
.EXAMPLE
  .\install.ps1
.EXAMPLE
  .\install.ps1 -Hosts codex -SkipDependencies
.EXAMPLE
  .\install.ps1 -AndrewCodeSource C:\dev\andrew-code
#>
[CmdletBinding()]
param(
  [ValidateSet('all', 'andrewcode', 'claude', 'codex', 'none')][string[]]$Hosts = @('all'),
  [string]$InstallRoot,
  [string]$AndrewCodeHome,
  [string]$AndrewCodeSource = $env:ANDREWCODE_SOURCE,
  [string]$Python,
  [string]$GitBash,
  [string]$CodexCommand,
  [string]$ClaudeCommand,
  [string]$AndrewCodeCommand,
  [ValidateSet('user', 'project', 'local')][string]$Scope = 'user',
  [string]$ProjectDirectory = (Get-Location).Path,
  [switch]$SkipDependencies,
  [switch]$SkipJq,
  [switch]$NoPathUpdate,
  [switch]$NoRegister,
  [switch]$DryRun,
  [switch]$NoPause
)

$ErrorActionPreference = 'Stop'
$SourceRoot = $PSScriptRoot
$Utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $Utf8

function Write-Utf8([string]$Path, [string]$Content) {
  [System.IO.Directory]::CreateDirectory((Split-Path -Parent $Path)) | Out-Null
  [System.IO.File]::WriteAllText($Path, $Content, $Utf8)
}

function Invoke-Checked([string]$Command, [string[]]$Arguments, [switch]$Capture) {
  # Windows PowerShell treats native stderr as ErrorRecord objects. Judge a
  # native command by its exit status, while retaining its actual diagnostics.
  $previous = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  $errorFile = [IO.Path]::GetTempFileName()
  try {
    $global:LASTEXITCODE = 0
    $output = @(& $Command @Arguments 2> $errorFile)
    $succeeded = $?
    $code = $LASTEXITCODE
    $diagnostics = [IO.File]::ReadAllText($errorFile)
  } finally {
    $ErrorActionPreference = $previous
    Remove-Item -LiteralPath $errorFile -Force
  }
  $message = ($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
  if (-not $succeeded -or $code -ne 0) {
    throw "$Command failed (exit $code). $message $diagnostics"
  }
  if ($diagnostics.Trim()) { Write-Host $diagnostics.Trim() -ForegroundColor Yellow }
  if ($Capture) { return $message }
  if ($message) { Write-Host $message }
}

function Resolve-Executable([string]$Name) {
  if ([string]::IsNullOrWhiteSpace($Name)) { return $null }
  if (Test-Path -LiteralPath $Name -PathType Leaf) { return [IO.Path]::GetFullPath($Name) }
  $found = Get-Command $Name -CommandType Application,ExternalScript -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($found) { return $found.Source }
  return $null
}

function Add-ProcessPath([string]$Directory) {
  if (-not $Directory -or -not (Test-Path -LiteralPath $Directory -PathType Container)) { return }
  $entries = @($env:PATH -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -ine $Directory.TrimEnd('\') })
  $env:PATH = (@($Directory) + $entries) -join ';'
}

function Refresh-ProcessPath {
  # Keep the caller's custom PATH, then include paths introduced by installers.
  foreach ($entry in @([Environment]::GetEnvironmentVariable('Path','Machine') -split ';') +
                     @([Environment]::GetEnvironmentVariable('Path','User') -split ';')) {
    if ($entry -and ($env:PATH -split ';') -notcontains $entry) { $env:PATH += ";$entry" }
  }
  foreach ($directory in @(
      (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'),
      (Join-Path $env:LOCALAPPDATA 'Programs\OpenAI\Codex\bin'),
      (Join-Path $env:LOCALAPPDATA 'Programs\Git\cmd'),
      (Join-Path $env:USERPROFILE '.local\bin'),
      (Join-Path $AndrewCodeHome 'bin')
  )) { Add-ProcessPath $directory }
}

function Add-UserPath([string]$Directory) {
  Add-ProcessPath $Directory
  if ($NoPathUpdate) { return }
  $current = [Environment]::GetEnvironmentVariable('Path','User')
  $entries = @($current -split ';' | Where-Object { $_ })
  if (@($entries | Where-Object { $_.TrimEnd('\') -ieq $Directory.TrimEnd('\') }).Count -eq 0) {
    [Environment]::SetEnvironmentVariable('Path', (@($Directory) + $entries) -join ';', 'User')
  }
}

function Install-WingetPackage([string]$Id, [switch]$Upgrade) {
  if ($SkipDependencies) { throw "$Id is required; -SkipDependencies prevents installing it." }
  $winget = Resolve-Executable 'winget.exe'
  if (-not $winget) { throw "WinGet is required to install $Id. Install Windows App Installer, or supply the dependency and rerun." }
  $verb = if ($Upgrade) { 'upgrade' } else { 'install' }
  Write-Host "Installing dependency: $Id"
  Invoke-Checked $winget @($verb, '--id', $Id, '--exact', '--source', 'winget',
      '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity', '--silent')
  Refresh-ProcessPath
}

function Find-Python {
  $candidates = @()
  if ($Python) {
    $candidate = Resolve-Executable $Python
    if (-not $candidate) { throw "The requested Python executable was not found: $Python" }
    $candidates += $candidate
  } else {
    foreach ($name in @('python.exe', 'python3.exe', 'py.exe')) {
      $candidate = Resolve-Executable $name
      if ($candidate -and $candidate -notmatch 'Microsoft\\WindowsApps\\python') { $candidates += $candidate }
    }
    foreach ($base in @((Join-Path $env:LOCALAPPDATA 'Programs\Python'), $env:ProgramFiles, 'C:\')) {
      if (Test-Path -LiteralPath $base) {
        $candidates += @(Get-ChildItem -LiteralPath $base -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
          Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName 'python.exe' })
      }
    }
  }
  foreach ($candidate in $candidates | Select-Object -Unique) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    $arguments = @('-c', 'import sys,json,sqlite3; assert sys.version_info >= (3,10); print(json.dumps(dict(path=sys.executable,version=list(sys.version_info[:3]))))')
    if ([IO.Path]::GetFileName($candidate) -ieq 'py.exe') { $arguments = @('-3') + $arguments }
    try {
      $details = Invoke-Checked $candidate $arguments -Capture | ConvertFrom-Json
      if ($details.path -and (Test-Path -LiteralPath $details.path -PathType Leaf)) { return $details.path }
    } catch {
      if ($Python) { throw "The selected Python must be 3.10+ with SQLite support. $($_.Exception.Message)" }
    }
  }
  return $null
}

function Ensure-Python {
  $executable = Find-Python
  if (-not $executable) {
    Install-WingetPackage 'Python.Python.3.14'
    $executable = Find-Python
  }
  if (-not $executable) { throw 'Python 3.10+ was not found after installation. Rerun in a new terminal or use -Python C:\path\python.exe.' }
  Add-ProcessPath (Split-Path -Parent $executable)
  return $executable
}

function Find-GitBash {
  if ($GitBash) {
    $candidate = Resolve-Executable $GitBash
    if (-not $candidate) { throw "The requested Git Bash was not found: $GitBash" }
    $candidates = @($candidate)
  } else {
    $candidates = @(
      (Join-Path $env:ProgramFiles 'Git\bin\bash.exe'),
      (Join-Path $env:LOCALAPPDATA 'Programs\Git\bin\bash.exe')
    )
    $git = Resolve-Executable 'git.exe'
    if ($git) { $candidates += Join-Path (Split-Path (Split-Path $git -Parent) -Parent) 'bin\bash.exe' }
  }
  foreach ($candidate in $candidates | Select-Object -Unique) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    if ($candidate -match '\\(System32|Sysnative)\\') { throw 'Select Git for Windows Bash, not WSL Bash.' }
    try {
      $platform = Invoke-Checked $candidate @('-c', 'uname -s') -Capture
      if ($platform -notmatch '^(MINGW|MSYS)') { throw "Expected Git for Windows, got $platform" }
      Invoke-Checked $candidate @('-c', 'command -v sed >/dev/null && command -v awk >/dev/null && command -v timeout >/dev/null') -Capture | Out-Null
      return $candidate
    } catch { if ($GitBash) { throw } }
  }
  return $null
}

function Ensure-GitBash {
  $executable = Find-GitBash
  if (-not $executable) {
    Install-WingetPackage 'Git.Git'
    $executable = Find-GitBash
  }
  if (-not $executable) { throw 'Git for Windows Bash is missing after installation. Use -GitBash C:\path\Git\bin\bash.exe.' }
  return $executable
}

function Ensure-Node([version]$Minimum) {
  $node = Resolve-Executable 'node.exe'
  $valid = $false
  if ($node) {
    try { $valid = [version](Invoke-Checked $node @('--version') -Capture).Trim().TrimStart('v') -ge $Minimum } catch {}
  }
  if (-not $valid) {
    Install-WingetPackage 'OpenJS.NodeJS.LTS' -Upgrade:([bool]$node)
    $node = Resolve-Executable 'node.exe'
    if (-not $node -or [version](Invoke-Checked $node @('--version') -Capture).Trim().TrimStart('v') -lt $Minimum) {
      throw "AndrewCode needs Node >= $Minimum. Install a supported Node version and rerun."
    }
  }
  return $node
}

function Install-CodexCli {
  if ($SkipDependencies) { throw 'Codex CLI is missing; -SkipDependencies prevents installing it.' }
  $downloadDir = Join-Path ([IO.Path]::GetTempPath()) ("fusion-codex-" + [guid]::NewGuid().ToString('N'))
  [IO.Directory]::CreateDirectory($downloadDir) | Out-Null
  $scriptPath = Join-Path $downloadDir 'install-codex.ps1'
  Write-Host 'Installing Codex CLI using the official OpenAI installer.'
  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
  Invoke-WebRequest -UseBasicParsing -Uri 'https://chatgpt.com/codex/install.ps1' -OutFile $scriptPath -TimeoutSec 60
  $oldNonInteractive = $env:CODEX_NON_INTERACTIVE
  try {
    $env:CODEX_NON_INTERACTIVE = '1'
    $shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    Invoke-Checked $shell @('-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass', '-File', $scriptPath)
    # Only the one file created by this function is removed. On failure it
    # remains available for inspection; no recursive shell deletion is used.
    Remove-Item -LiteralPath $scriptPath -Force
    [IO.Directory]::Delete($downloadDir)
  } finally { $env:CODEX_NON_INTERACTIVE = $oldNonInteractive }
  Refresh-ProcessPath
}

function Install-AndrewCodeCli {
  if ($SkipDependencies) { throw 'AndrewCode CLI is missing; -SkipDependencies prevents installing it.' }
  $source = $AndrewCodeSource
  if (-not $source -and (Test-Path -LiteralPath 'C:\dev\andrew-code\install.ps1')) { $source = 'C:\dev\andrew-code' }
  if (-not $source -or -not (Test-Path -LiteralPath (Join-Path $source 'scripts\install-andrewcode.mjs'))) {
    throw 'AndrewCode is a personal fork. Supply -AndrewCodeSource C:\path\andrew-code with its native installer; upstream Kimi is not substituted.'
  }
  $source = [IO.Path]::GetFullPath($source)
  $manifest = Get-Content -LiteralPath (Join-Path $source 'package.json') -Encoding UTF8 -Raw | ConvertFrom-Json
  $minimum = [version]'24.15.0'
  if ($manifest.engines.node -match '(\d+\.\d+\.\d+)') { $minimum = [version]$Matches[1] }
  Ensure-Node $minimum | Out-Null
  $pnpmSpec = $manifest.packageManager
  if ($pnpmSpec -notmatch '^pnpm@\d+\.\d+\.\d+') { throw 'AndrewCode package.json must declare a pinned pnpm packageManager.' }
  $pnpmSpec = ($pnpmSpec -split '\+')[0]
  $pnpm = Resolve-Executable 'pnpm.cmd'
  if (-not $pnpm -or (Invoke-Checked $pnpm @('--version') -Capture).Trim() -ne ($pnpmSpec -split '@')[1]) {
    $npm = Resolve-Executable 'npm.cmd'
    if (-not $npm) { throw 'npm.cmd was not found after Node installation.' }
    $tools = Join-Path $InstallRoot 'tools\npm'
    Invoke-Checked $npm @('install', '--global', '--prefix', $tools, $pnpmSpec)
    Add-UserPath $tools
  }
  Write-Host "Building and installing AndrewCode from $source"
  $oldAndrewHome = $env:ANDREWCODE_HOME
  try {
    $env:ANDREWCODE_HOME = $AndrewCodeHome
    $shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    # Use the fork's full native install, including dependency install and build.
    Invoke-Checked $shell @('-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $source 'install.ps1'),
                           '-BinDir', (Join-Path $AndrewCodeHome 'bin'))
  } finally { $env:ANDREWCODE_HOME = $oldAndrewHome }
  Refresh-ProcessPath
}

function Ensure-Host([string]$Name, [string]$Override) {
  $command = Resolve-Executable $Override
  if ($Override -and -not $command) { throw "The requested $Name executable was not found: $Override" }
  if (-not $command) { $command = Resolve-Executable $Name }
  if (-not $command) {
    switch ($Name) {
      'codex' { Install-CodexCli }
      'claude' { Install-WingetPackage 'Anthropic.ClaudeCode' }
      'andrewcode' { Install-AndrewCodeCli }
    }
    $command = Resolve-Executable $Name
  }
  if (-not $command) { throw "$Name was not found after setup. Rerun in a new terminal or supply its executable path." }
  $version = Invoke-Checked $command @('--version') -Capture
  Write-Host ('{0}: {1}' -f $Name,$version)
  return $command
}

function Same-Path([string]$First, [string]$Second) {
  if (-not $First -or -not $Second) { return $false }
  $First = $First -replace '^\\\\\?\\', ''
  $Second = $Second -replace '^\\\\\?\\', ''
  return [IO.Path]::GetFullPath($First).TrimEnd('\','/') -ieq [IO.Path]::GetFullPath($Second).TrimEnd('\','/')
}

function Set-FusionMarketplace([string]$HostName, [string]$Command, [string]$CurrentRoot, [string]$PluginRoot, [string]$Market) {
  if ($CurrentRoot -and (Same-Path $CurrentRoot $PluginRoot)) { return $null }
  $addFlags = if ($HostName -eq 'codex') { @('--json') } else { @('--scope',$Scope) }
  $removeFlags = if ($HostName -eq 'codex') { @('--json') } else { @('--scope',$Scope) }
  if ($CurrentRoot) {
    # Relocation is restricted to a marketplace containing only this plugin.
    # Never remove an unrelated/multi-plugin marketplace to make room for it.
    $oldManifest = Join-Path $CurrentRoot '.claude-plugin\marketplace.json'
    if (-not (Test-Path -LiteralPath $oldManifest -PathType Leaf)) { throw "Cannot safely relocate $Market from $CurrentRoot; its local manifest is missing." }
    $old = Get-Content -LiteralPath $oldManifest -Encoding UTF8 -Raw | ConvertFrom-Json
    if ($old.name -ne $Market -or @($old.plugins).Count -ne 1 -or $old.plugins[0].name -ne 'fusion' -or $old.plugins[0].source -ne './') {
      throw "Marketplace $Market is not exclusively owned by Fusion. Existing configuration was preserved."
    }
    Invoke-Checked $Command (@('plugin','marketplace','remove',$Market) + $removeFlags)
  }
  try { Invoke-Checked $Command (@('plugin','marketplace','add',$PluginRoot) + $addFlags) }
  catch {
    $failure = $_.Exception.Message
    if ($CurrentRoot) {
      try { Invoke-Checked $Command (@('plugin','marketplace','add',$CurrentRoot) + $addFlags) }
      catch { throw "$failure Restoring the prior marketplace also failed: $($_.Exception.Message)" }
    }
    throw $failure
  }
  return $CurrentRoot
}

function Restore-FusionMarketplace([string]$HostName, [string]$Command, [string]$PreviousRoot, [string]$Market) {
  if (-not $PreviousRoot) { return }
  if ($HostName -eq 'codex') {
    Invoke-Checked $Command @('plugin','marketplace','remove',$Market,'--json')
    Invoke-Checked $Command @('plugin','marketplace','add',$PreviousRoot,'--json')
    Invoke-Checked $Command @('plugin','add',"fusion@$Market",'--json')
  } else {
    Invoke-Checked $Command @('plugin','marketplace','remove',$Market,'--scope',$Scope)
    Invoke-Checked $Command @('plugin','marketplace','add',$PreviousRoot,'--scope',$Scope)
    Invoke-Checked $Command @('plugin','install',"fusion@$Market",'--scope',$Scope)
  }
}

function Register-Codex([string]$Command, [string]$PluginRoot, [string]$Market) {
  $listing = Invoke-Checked $Command @('plugin','marketplace','list','--json') -Capture | ConvertFrom-Json
  $existing = @($listing.marketplaces | Where-Object { $_.name -eq $Market })
  $oldRoot = if ($existing.Count) { $existing[0].root } else { $null }
  $relocated = Set-FusionMarketplace 'codex' $Command $oldRoot $PluginRoot $Market
  try {
    Invoke-Checked $Command @('plugin','add',"fusion@$Market",'--json')
    $listingPath = Join-Path $InstallRoot 'logs\codex-plugins.json'
    Write-Utf8 $listingPath (Invoke-Checked $Command @('plugin','list','--marketplace',$Market,'--json') -Capture)
    $codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
    Invoke-Checked $PythonExe @('-B', (Join-Path $SourceRoot 'scripts\fusion_install.py'), 'verify-host',
      '--host','codex','--root',$PluginRoot,'--listing',$listingPath,'--cache',(Join-Path $codexHome 'plugins\cache'))
  } catch {
    $failure = $_.Exception.Message
    if ($failure -match 'failed to back up plugin cache entry' -and $failure -match 'Access is denied|os error 5') {
      $failure += ' The Fusion cache may be in use. Close Codex completely, then rerun install-codex.ps1 from a separate PowerShell window.'
    }
    try { Restore-FusionMarketplace 'codex' $Command $relocated $Market }
    catch { throw "$failure Restoring the prior Fusion marketplace failed: $($_.Exception.Message)" }
    throw $failure
  }
}

function Register-Claude([string]$Command, [string]$PluginRoot, [string]$Market) {
  Invoke-Checked $Command @('plugin','validate',$PluginRoot)
  $listingData = Invoke-Checked $Command @('plugin','marketplace','list','--json') -Capture | ConvertFrom-Json
  $listing = @($listingData)
  $existing = @($listing | Where-Object { $_.name -eq $Market })
  $oldRoot = if ($existing.Count) { $existing[0].path } else { $null }
  $relocated = Set-FusionMarketplace 'claude' $Command $oldRoot $PluginRoot $Market
  try {
    Invoke-Checked $Command @('plugin','install',"fusion@$Market",'--scope',$Scope)
    Invoke-Checked $Command @('plugin','update',"fusion@$Market",'--scope',$Scope)
    $installedData = Invoke-Checked $Command @('plugin','list','--json') -Capture | ConvertFrom-Json
    $installed = @($installedData)
    $enabled = @($installed | Where-Object { $_.id -eq "fusion@$Market" -and $_.scope -eq $Scope -and $_.enabled -eq $true })
    if ($enabled.Count -eq 0) { Invoke-Checked $Command @('plugin','enable',"fusion@$Market",'--scope',$Scope) }
    $listingPath = Join-Path $InstallRoot 'logs\claude-plugins.json'
    Write-Utf8 $listingPath (Invoke-Checked $Command @('plugin','list','--json') -Capture)
    $marketplacesPath = Join-Path $InstallRoot 'logs\claude-marketplaces.json'
    Write-Utf8 $marketplacesPath (Invoke-Checked $Command @('plugin','marketplace','list','--json') -Capture)
    Invoke-Checked $PythonExe @('-B', (Join-Path $SourceRoot 'scripts\fusion_install.py'), 'verify-host',
      '--host','claude','--root',$PluginRoot,'--listing',$listingPath,'--scope',$Scope,'--marketplaces',$marketplacesPath)
  } catch {
    $failure = $_.Exception.Message
    try { Restore-FusionMarketplace 'claude' $Command $relocated $Market }
    catch { throw "$failure Restoring the prior Fusion marketplace failed: $($_.Exception.Message)" }
    throw $failure
  }
}

function Invoke-FusionInstall {
  $selected = @($Hosts | Select-Object -Unique)
  if ($selected.Count -gt 1 -and ($selected -contains 'all' -or $selected -contains 'none')) {
    Write-Host 'Use all/none alone, or list individual hosts.' -ForegroundColor Red
    return 2
  }
  if ($selected -contains 'all') { $selected = @('andrewcode','claude','codex') }
  if ($NoRegister -or $selected -contains 'none') { $selected = @() }
  if (-not $InstallRoot) { $script:InstallRoot = Join-Path $env:LOCALAPPDATA 'Fusion' }
  $script:InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
  if (-not $AndrewCodeHome) {
    $script:AndrewCodeHome = $env:ANDREWCODE_HOME
    if (-not $AndrewCodeHome) { $script:AndrewCodeHome = $env:KIMI_CODE_HOME }
    if (-not $AndrewCodeHome) { $script:AndrewCodeHome = $env:KIMI_HOME }
    if (-not $AndrewCodeHome) { $script:AndrewCodeHome = Join-Path $env:USERPROFILE '.andrewcode' }
  }
  $script:AndrewCodeHome = [IO.Path]::GetFullPath($AndrewCodeHome)
  $sourceFull = [IO.Path]::GetFullPath($SourceRoot).TrimEnd('\') + '\'
  $installFull = $InstallRoot.TrimEnd('\') + '\'
  if ($sourceFull.StartsWith($installFull,[StringComparison]::OrdinalIgnoreCase) -or
      $installFull.StartsWith($sourceFull,[StringComparison]::OrdinalIgnoreCase)) {
    Write-Host 'InstallRoot must be separate from the source checkout.' -ForegroundColor Red
    return 1
  }
  if ($DryRun) {
    Write-Host "Fusion dry run: source=$SourceRoot"
    Write-Host "Install program and fusion command into $InstallRoot"
    Write-Host 'Check Python 3.10+, Git for Windows Bash and jq; install missing dependencies with WinGet.'
    Write-Host ("Register hosts: " + ($selected -join ', '))
    Write-Host 'Missing Codex/Claude CLIs are installed through their official distributions; AndrewCode uses -AndrewCodeSource.'
    Write-Host "AndrewCode home: $AndrewCodeHome; Claude scope: $Scope"
    Write-Host "SkipDependencies=$SkipDependencies; SkipJq=$SkipJq; NoPathUpdate=$NoPathUpdate"
    return 0
  }
  $report = [ordered]@{
    startedAt = [DateTime]::UtcNow.ToString('o'); source = $SourceRoot; installRoot = $InstallRoot
    requestedHosts = $selected; runtime = 'pending'; dependencies = @(); hosts = [ordered]@{}; errors = @()
  }
  foreach ($name in $selected) { $report.hosts[$name] = @{ status='pending'; detail='' } }
  $mutex = $null
  $ownsMutex = $false
  $complete = $false
  try {
    if ($env:OS -ne 'Windows_NT') { throw 'Use install.sh on macOS/Linux. This installer requires Windows.' }
    $digest = [Security.Cryptography.SHA256]::Create()
    try { $key = ([BitConverter]::ToString($digest.ComputeHash([Text.Encoding]::UTF8.GetBytes($InstallRoot.ToLowerInvariant())))) -replace '-', '' }
    finally { $digest.Dispose() }
    $mutex = New-Object System.Threading.Mutex($false, "Local\FusionInstall-$key")
    try { $ownsMutex = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { throw "Another Fusion installer is running for $InstallRoot." }
    Refresh-ProcessPath
    $script:PythonExe = Ensure-Python
    $script:BashExe = Ensure-GitBash
    $env:PYTHONUTF8 = '1'
    $env:FUSION_PYTHON = $PythonExe
    $env:FUSION_BASH = $BashExe
    $report.dependencies += @{ name='python'; path=$PythonExe; status='ready' }
    $report.dependencies += @{ name='git-bash'; path=$BashExe; status='ready' }
    if (-not $SkipJq) {
      $jq = Resolve-Executable 'jq.exe'
      if (-not $jq -and -not $SkipDependencies) { Install-WingetPackage 'jqlang.jq'; $jq = Resolve-Executable 'jq.exe' }
      if ($jq) { Invoke-Checked $jq @('--version'); $report.dependencies += @{name='jq';status='ready';path=$jq} }
      elseif ($SkipDependencies) { Write-Warning 'jq is absent; the legacy learning ledger will use its fallback.'; $report.dependencies += @{name='jq';status='optional-missing'} }
      else { throw 'jq was not found after dependency installation. Use -SkipJq for the fallback ledger.' }
    }
    $runtime = Invoke-Checked $PythonExe @('-B',(Join-Path $SourceRoot 'scripts\fusion_install.py'),'runtime',
      '--root',$SourceRoot,'--destination',$InstallRoot,'--python',$PythonExe,'--bash',$BashExe) -Capture | ConvertFrom-Json
    $report.runtime = 'verified'
    $report.verification = $runtime
    Add-UserPath $runtime.bin
    Write-Host "Fusion installed: $($runtime.forms) coding forms, $($runtime.mcpTools) coordination tools."
    foreach ($name in $selected) {
      try {
        switch ($name) {
          'andrewcode' {
            $command = Ensure-Host 'andrewcode' $AndrewCodeCommand
            Invoke-Checked $PythonExe @('-B',(Join-Path $SourceRoot 'scripts\install-kimi.py'),'--root',$runtime.root,
              '--home',$AndrewCodeHome,'--python',$PythonExe,'--bash',$BashExe)
            Invoke-Checked $PythonExe @('-B',(Join-Path $SourceRoot 'scripts\fusion_install.py'),'verify',
              '--root',(Join-Path $AndrewCodeHome 'plugins\managed\fusion'),'--python',$PythonExe)
          }
          'claude' {
            $command = Ensure-Host 'claude' $ClaudeCommand
            Push-Location -LiteralPath $ProjectDirectory
            try { Register-Claude $command $runtime.root $runtime.marketplace } finally { Pop-Location }
          }
          'codex' {
            $command = Ensure-Host 'codex' $CodexCommand
            Register-Codex $command $runtime.root $runtime.marketplace
          }
        }
        $report.hosts[$name] = @{status='installed';detail='Registry and installed package verified'}
        Write-Host "Fusion registered with $name." -ForegroundColor Green
      } catch {
        $report.hosts[$name] = @{status='failed';detail=$_.Exception.Message}
        $report.errors += "$name : $($_.Exception.Message)"
        Write-Host "$name installation failed: $($_.Exception.Message)" -ForegroundColor Red
      }
    }
    $complete = $report.errors.Count -eq 0
  } catch {
    $report.errors += $_.Exception.Message
    Write-Host "Fusion installation failed: $($_.Exception.Message)" -ForegroundColor Red
  } finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
    $report.completedAt = [DateTime]::UtcNow.ToString('o')
    $report.status = if ($complete) { 'complete' } else { 'failed' }
    try {
      $reportPath = Join-Path $InstallRoot 'logs\latest-install.json'
      Write-Utf8 $reportPath ($report | ConvertTo-Json -Depth 10)
      Write-Host "Installation report: $reportPath"
    } catch { $complete = $false; Write-Host "Could not save installation report: $($_.Exception.Message)" -ForegroundColor Red }
  }
  if (-not $complete) { return 1 }
  Write-Host 'Fusion installation complete. Open a new terminal to use: fusion forms --json' -ForegroundColor Green
  if ($selected -contains 'andrewcode') { Write-Host 'AndrewCode: /plugins reload, then /new, then /fusion:sidekick' }
  if ($selected -contains 'claude') { Write-Host 'Claude Code: restart the client, then /fusion:sidekick' }
  if ($selected -contains 'codex') { Write-Host 'Codex: start a new chat and invoke fusion-sidekick or fusion-team' }
  Write-Host 'Sign in to missing host accounts yourself. This installation made no model calls.'
  return 0
}

# Dot-sourcing exposes functions for offline installer tests without installing.
if ($MyInvocation.InvocationName -ne '.') { exit (Invoke-FusionInstall) }
