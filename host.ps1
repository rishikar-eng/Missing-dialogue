# Dialogue QC - one-command HOSTED launch (backend + ngrok tunnel).
#
# Serves the built React UI + API from the local Python backend, protected by an API key,
# and exposes it publicly through an ngrok tunnel so teammates can use it from a browser.
# Files are read on THIS machine (from -DataRoot) - nothing uploads.
#
# Usage:
#   .\host.ps1 -DataRoot "D:\Episodes"                 # ephemeral ngrok URL (changes each run)
#   .\host.ps1 -DataRoot "D:\Episodes" -Domain my-name.ngrok-free.app   # stable free static domain
#
# First-time setup (once): create a free ngrok account, then:
#   ngrok config add-authtoken <YOUR_TOKEN>            # from https://dashboard.ngrok.com
#
# Stop with Ctrl+C (both the backend and the tunnel are torn down).

param(
  [Parameter(Mandatory = $true)] [string]$DataRoot,
  [int]$Port = 8765,
  [string]$Domain = "",          # your reserved ngrok-free static domain (optional but recommended)
  [int]$VadWorkers = 2           # sequential-ish VAD; raise if the host has plenty of RAM
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# --- sanity checks ---------------------------------------------------------
if (-not (Test-Path $DataRoot)) { throw "DataRoot not found: $DataRoot" }
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "No .venv found. Run:  python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt" }
if (-not (Test-Path (Join-Path $root "dist\index.html"))) {
  Write-Host "Building the UI (dist/ missing)..." -ForegroundColor Yellow
  npm run build:ui
}

# resolve ngrok (PATH after a shell restart, else the winget install location)
$ngrok = (Get-Command ngrok -ErrorAction SilentlyContinue).Source
if (-not $ngrok) {
  $ngrok = (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter ngrok.exe -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
}
if (-not $ngrok) { throw "ngrok not found. Install it:  winget install Ngrok.Ngrok" }

# --- stable API key (persisted so the share link stays valid across restarts) ---
$keyDir = Join-Path $env:LOCALAPPDATA "dialogue-qc"
New-Item -ItemType Directory -Force -Path $keyDir | Out-Null
$keyFile = Join-Path $keyDir "host-key.txt"
if (Test-Path $keyFile) {
  $apiKey = (Get-Content $keyFile -Raw).Trim()
} else {
  $bytes = New-Object 'System.Byte[]' 24
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $apiKey = ([Convert]::ToBase64String($bytes)) -replace '[^A-Za-z0-9]', ''
  Set-Content -Path $keyFile -Value $apiKey -NoNewline -Encoding ascii
  Write-Host "Generated a new API key (saved to $keyFile)." -ForegroundColor Green
}

# --- start the backend -----------------------------------------------------
$env:DQC_API_KEY    = $apiKey
$env:DQC_DATA_ROOT  = $DataRoot
$env:DQC_VAD_WORKERS = "$VadWorkers"
$env:DQC_PORT       = "$Port"

Write-Host "Starting backend on 127.0.0.1:$Port  (data root: $DataRoot)..." -ForegroundColor Cyan
$backend = Start-Process -FilePath $venvPy `
  -ArgumentList @("-m", "uvicorn", "backend.server:app", "--host", "127.0.0.1", "--port", "$Port") `
  -WorkingDirectory $root -PassThru -NoNewWindow

# wait for it to answer
$up = $false
foreach ($i in 1..30) {
  Start-Sleep 1
  try { if ((Invoke-WebRequest "http://127.0.0.1:$Port/api/healthz" -TimeoutSec 1 -UseBasicParsing).StatusCode -eq 200) { $up = $true; break } } catch {}
}
if (-not $up) { $backend | Stop-Process -Force -ErrorAction SilentlyContinue; throw "Backend did not come up on port $Port." }

# --- start the tunnel ------------------------------------------------------
# Capture ngrok's own log: when the tunnel fails we must show ITS reason, not guess.
# (A stale agent fails with ERR_NGROK_121 "agent too old", which looks nothing like an
# auth problem -- an earlier version of this script blamed the authtoken and sent us
# chasing the wrong thing.)
$ngrokLog = Join-Path $env:TEMP "dqc-ngrok.log"
if (Test-Path $ngrokLog) { Remove-Item $ngrokLog -Force -ErrorAction SilentlyContinue }
$ngrokArgs = @("http", "$Port", "--log", "stdout")
if ($Domain) { $ngrokArgs += @("--url", $Domain) }
Write-Host "Opening ngrok tunnel..." -ForegroundColor Cyan
$tunnel = Start-Process -FilePath $ngrok -ArgumentList $ngrokArgs -PassThru -NoNewWindow `
  -RedirectStandardOutput $ngrokLog -RedirectStandardError "$ngrokLog.err"

# read the public URL from ngrok's local API
$publicUrl = $null
foreach ($i in 1..20) {
  Start-Sleep 1
  try {
    $t = Invoke-RestMethod "http://127.0.0.1:4040/api/tunnels" -TimeoutSec 1
    $publicUrl = ($t.tunnels | Where-Object { $_.proto -eq "https" } | Select-Object -First 1).public_url
    if ($publicUrl) { break }
  } catch {}
  if ($tunnel.HasExited) { break }  # ngrok died - stop waiting, report why
}

function Stop-All {
  Write-Host "`nShutting down..." -ForegroundColor Yellow
  if ($tunnel)  { Stop-Process -Id $tunnel.Id  -Force -ErrorAction SilentlyContinue }
  if ($backend) { Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue }
}

if (-not $publicUrl) {
  Stop-All
  # Surface ngrok's real error verbatim, and map the ones we've actually hit.
  $errText = @()
  foreach ($f in @($ngrokLog, "$ngrokLog.err")) {
    if (Test-Path $f) { $errText += (Select-String -Path $f -Pattern 'ERR_NGROK_\d+|err="[^"]+"|ERROR:' -EA SilentlyContinue | ForEach-Object { $_.Line.Trim() }) }
  }
  $joined = ($errText | Select-Object -Unique -First 6) -join "`n  "
  Write-Host "`nngrok said:" -ForegroundColor Red
  Write-Host "  $joined" -ForegroundColor DarkGray
  $hint = "See the ngrok output above."
  if ($joined -match 'ERR_NGROK_121|too old')  { $hint = "Your ngrok agent is TOO OLD. Fix:  ngrok update" }
  elseif ($joined -match 'ERR_NGROK_4018|not authenticated') { $hint = "ngrok isn't authenticated. Fix:  ngrok config add-authtoken <YOUR_TOKEN>  (free at https://dashboard.ngrok.com)" }
  elseif ($joined -match 'ERR_NGROK_(105|108)') { $hint = "Authtoken rejected, or another ngrok agent/session is already running." }
  throw "Could not open the ngrok tunnel. $hint"
}

$shareLink = "$publicUrl/?key=$apiKey"
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " Dialogue QC is LIVE. Share this link with the team:" -ForegroundColor Green
Write-Host ""
Write-Host "   $shareLink" -ForegroundColor White
Write-Host ""
Write-Host " (The ?key= authenticates them; after first load it's stored" -ForegroundColor DarkGray
Write-Host "  in their browser and dropped from the address bar.)" -ForegroundColor DarkGray
Write-Host " ngrok inspector: http://127.0.0.1:4040   |   Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host "============================================================" -ForegroundColor Green

try {
  Set-Clipboard -Value $shareLink -ErrorAction SilentlyContinue
  Write-Host "(link copied to clipboard)" -ForegroundColor DarkGray
} catch {}

# keep running until Ctrl+C; tear both down on exit
try {
  while ($true) {
    Start-Sleep 2
    if ($backend.HasExited) { Write-Host "Backend exited." -ForegroundColor Red; break }
    if ($tunnel.HasExited)  { Write-Host "Tunnel exited." -ForegroundColor Red; break }
  }
} finally {
  Stop-All
}
