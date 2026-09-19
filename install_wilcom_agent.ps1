$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Root "wilcom-agent-venv"
$Config = Join-Path $Root "wilcom_agent_config.json"
$Example = Join-Path $Root "wilcom_agent_config.example.json"

if (-not (Test-Path $Venv)) {
    py -3 -m venv $Venv
}

& (Join-Path $Venv "Scripts\python.exe") -m pip install `
    -r (Join-Path $Root "warehouse-agent-requirements.txt")

if (-not (Test-Path $Config)) {
    Copy-Item $Example $Config
    Write-Host ""
    Write-Host "Created $Config"
    Write-Host "Replace the token and four placeholder Wilcom device names before starting."
}

$Startup = [Environment]::GetFolderPath("Startup")
$Launcher = Join-Path $Startup "Wilcom Warehouse Agent.cmd"
$Pythonw = Join-Path $Venv "Scripts\pythonw.exe"
$Agent = Join-Path $Root "warehouse_wilcom_agent.py"

@"
@echo off
start "" "$Pythonw" "$Agent" --config "$Config"
"@ | Set-Content -Path $Launcher -Encoding ASCII

Write-Host ""
Write-Host "Startup launcher installed at:"
Write-Host $Launcher
Write-Host ""
Write-Host "The helper starts after Windows sign-in. Keep the desktop unlocked,"
Write-Host "EmbroideryHub running, and Google Drive mounted at the configured path."
