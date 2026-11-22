#requires -Version 5.1
# Install Fusion runtime/dependencies and register the andrewcode host selection.
[CmdletBinding()]
param(
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

& (Join-Path $PSScriptRoot 'install.ps1') -Hosts 'andrewcode' @PSBoundParameters
exit $LASTEXITCODE
