# Deploys Justin's Gmail OAuth token to Render as GOOGLE_GMAIL_TOKEN_JSON.
# Prefer gmail_token.json (from: python generate_token.py --gmail).
# Falls back to token.json only if it is authenticated as justin.eckard@jrcogolf.com.
$ErrorActionPreference = "Stop"
$RenderExe = "C:\Users\eckar\AppData\Local\Microsoft\WinGet\Packages\Render.CLI_Microsoft.Winget.Source_8wekyb3d8bbwe\render.exe"
$RequiredAccount = "justin.eckard@jrcogolf.com"
$GmailTokenPath = Join-Path $PSScriptRoot "gmail_token.json"
$MainTokenPath = Join-Path $PSScriptRoot "token.json"

if (Test-Path $GmailTokenPath) {
  $TokenPath = $GmailTokenPath
} elseif (Test-Path $MainTokenPath) {
  $TokenPath = $MainTokenPath
} else {
  throw "Missing gmail_token.json / token.json — run: python generate_token.py --gmail"
}

$tokenRaw = (Get-Content $TokenPath -Raw).Trim()
$tokenObj = $tokenRaw | ConvertFrom-Json
$scopes = @($tokenObj.scopes) -join " "
if ($scopes -notmatch "gmail\.compose") { throw "$TokenPath lacks gmail.compose scopes" }

$account = [string]$tokenObj.account
if (-not $account) {
  Write-Host "token has no account field; verifying via Gmail API..."
  $account = (& python -c @"
import json
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
info=json.load(open(r'$TokenPath', encoding='utf-8'))
creds=Credentials.from_authorized_user_info(info)
if creds.expired and creds.refresh_token: creds.refresh(Request())
gmail=build('gmail','v1',credentials=creds,cache_discovery=False)
print((gmail.users().getProfile(userId='me').execute() or {}).get('emailAddress') or '')
"@).Trim()
}
if ($account.ToLower() -ne $RequiredAccount) {
  throw "Token is for '$account' but digitizing must use $RequiredAccount. Run: python generate_token.py --gmail"
}

Write-Host "Using $TokenPath ($account)"
Write-Host "Logging into Render (browser will open)..."
& $RenderExe login
if ($LASTEXITCODE -ne 0) { throw "render login failed" }

Write-Host "Finding machine-scheduler-backend service..."
$servicesJson = & $RenderExe services -o json 2>$null
if (-not $servicesJson) { $servicesJson = & $RenderExe services --output json }
$services = $servicesJson | ConvertFrom-Json
$svc = $services | Where-Object {
  $_.Service -and ($_.Service.name -eq "machine-scheduler-backend" -or $_.Service.serviceDetails.url -match "machine-scheduler-backend")
} | Select-Object -First 1
if (-not $svc) {
  $svc = $services | Where-Object { $_.name -eq "machine-scheduler-backend" } | Select-Object -First 1
}
$id = if ($svc.Service) { $svc.Service.id } elseif ($svc.id) { $svc.id } else { $null }
if (-not $id) {
  Write-Host ($services | ConvertTo-Json -Depth 4)
  throw "Could not find machine-scheduler-backend service id"
}
Write-Host "Service id: $id"

$cfg = Get-Content "$env:USERPROFILE\.render\cli.yaml" -Raw
if ($cfg -notmatch "key:\s*(rnd_\S+)") { throw "No Render API key after login" }
$apiKey = $Matches[1]
$headers = @{ Authorization = "Bearer $apiKey"; Accept = "application/json"; "Content-Type" = "application/json" }
$body = @{ value = $tokenRaw } | ConvertTo-Json -Compress
Invoke-RestMethod -Method Put -Uri "https://api.render.com/v1/services/$id/env-vars/GOOGLE_GMAIL_TOKEN_JSON" -Headers $headers -Body $body | Out-Null
Write-Host "Updated GOOGLE_GMAIL_TOKEN_JSON"
Invoke-RestMethod -Method Put -Uri "https://api.render.com/v1/services/$id/env-vars/NEEDLEPOINT_GMAIL_ACCOUNT" -Headers $headers -Body (@{ value = $RequiredAccount } | ConvertTo-Json -Compress) | Out-Null
Write-Host "Updated NEEDLEPOINT_GMAIL_ACCOUNT=$RequiredAccount"

Write-Host "Triggering deploy..."
& $RenderExe deploys create $id --wait 2>&1
Write-Host "Done. Checking production token-status..."
Start-Sleep -Seconds 8
Invoke-RestMethod "https://machine-scheduler-backend.onrender.com/api/drive/token-status" | ConvertTo-Json
