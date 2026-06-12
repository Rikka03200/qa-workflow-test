# scripts/load-config.ps1
#
# Optional helper: parse config/config.local.yaml and set credentials as
# environment variables for the CURRENT PowerShell process only.
# Does NOT touch Windows registry (no permanent env changes).
#
# You typically do NOT need this script. The main path is:
#   `python scripts/mcp-atlassian-wrapper.py` invoked automatically by .mcp.json
# Use this only when you want to manually run curl/uvx with the same env.
#
# Usage:
#   .\scripts\load-config.ps1

$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition

# Delegate to _load_env.py (more robust than parsing yaml in PS)
$output = python (Join-Path $ScriptRoot "_load_env.py") 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error ($output -join "`n")
    exit 1
}

$count = 0
foreach ($line in $output) {
    if ($line -match '^([A-Z_]+)=(.*)$') {
        Set-Item -Path "env:$($Matches[1])" -Value $Matches[2]
        $count++
    }
}

Write-Host "Loaded $count process-scoped env vars (cleared when terminal closes):" -ForegroundColor Green
if ($env:JIRA_URL)             { Write-Host "  [OK] JIRA_URL = $($env:JIRA_URL)" }
if ($env:JIRA_PERSONAL_TOKEN)  { Write-Host "  [OK] JIRA_PERSONAL_TOKEN = (len=$($env:JIRA_PERSONAL_TOKEN.Length))" }
if ($env:JIRA_USERNAME)        { Write-Host "  [OK] JIRA_USERNAME = $($env:JIRA_USERNAME)" }
if ($env:CONFLUENCE_URL)       { Write-Host "  [OK] CONFLUENCE_URL = $($env:CONFLUENCE_URL)" }
if ($env:CONFLUENCE_PERSONAL_TOKEN) { Write-Host "  [OK] CONFLUENCE_PERSONAL_TOKEN = (len=$($env:CONFLUENCE_PERSONAL_TOKEN.Length))" }
if ($env:READ_ONLY_MODE)       { Write-Host "  [OK] READ_ONLY_MODE = $($env:READ_ONLY_MODE)" }

Write-Host ""
Write-Host "Note: These vars only live in this PowerShell process. Windows global env is untouched." -ForegroundColor DarkGray
