# install-windows.ps1 — Set up VoxHerd Bridge on Windows
#
# One-command setup: creates venv, installs deps, installs hooks.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1

$ErrorActionPreference = "Stop"

# Locate repo root
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir

Write-Host "=== VoxHerd Windows Setup ===" -ForegroundColor Cyan
Write-Host "Repo root: $RepoRoot"
Write-Host ""

# -----------------------------------------------------------------------
# 1. Check prerequisites
# -----------------------------------------------------------------------
Write-Host "Checking prerequisites..." -ForegroundColor Yellow

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python3 = Get-Command python3 -ErrorAction SilentlyContinue
    if ($python3) {
        $python = $python3
    } else {
        Write-Host "ERROR: Python 3.11+ is required but not found in PATH." -ForegroundColor Red
        Write-Host "Download from: https://www.python.org/downloads/" -ForegroundColor Red
        exit 1
    }
}

$pyVersion = & $python.Source --version 2>&1
Write-Host "  Python: $pyVersion"

$claude = Get-Command claude -ErrorAction SilentlyContinue
if ($claude) {
    Write-Host "  Claude CLI: found"
} else {
    Write-Host "  Claude CLI: not found (hooks will work but summaries will use API fallback)" -ForegroundColor Yellow
}

$tailscale = $null
if (Test-Path "C:\Program Files\Tailscale\tailscale.exe") {
    $tailscale = "C:\Program Files\Tailscale\tailscale.exe"
} else {
    $tailscale = Get-Command tailscale -ErrorAction SilentlyContinue
}
if ($tailscale) {
    Write-Host "  Tailscale: found"
} else {
    Write-Host "  Tailscale: not found (optional, for remote access)" -ForegroundColor Yellow
}

Write-Host ""

# -----------------------------------------------------------------------
# 2. Create Python virtual environment
# -----------------------------------------------------------------------
Write-Host "Setting up Python virtual environment..." -ForegroundColor Yellow

$venvDir = Join-Path $RepoRoot "bridge\.venv"
if (-not (Test-Path $venvDir)) {
    & $python.Source -m venv $venvDir
    Write-Host "  Created venv at: $venvDir"
} else {
    Write-Host "  Venv already exists at: $venvDir"
}

$pipExe = Join-Path $venvDir "Scripts\pip.exe"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"

# -----------------------------------------------------------------------
# 3. Install bridge server dependencies
# -----------------------------------------------------------------------
Write-Host "Installing bridge server dependencies..." -ForegroundColor Yellow

$bridgeReqs = Join-Path $RepoRoot "bridge\requirements.txt"
if (Test-Path $bridgeReqs) {
    & $pipExe install -q -r $bridgeReqs
    Write-Host "  Bridge dependencies installed"
}

# -----------------------------------------------------------------------
# 4. Install tray app dependencies
# -----------------------------------------------------------------------
Write-Host "Installing tray app dependencies..." -ForegroundColor Yellow

$trayReqs = Join-Path $RepoRoot "windows\requirements-windows.txt"
if (Test-Path $trayReqs) {
    & $pipExe install -q -r $trayReqs
    Write-Host "  Tray app dependencies installed"
}

# -----------------------------------------------------------------------
# 5. Create config directory
# -----------------------------------------------------------------------
Write-Host "Creating config directories..." -ForegroundColor Yellow

$appDataDir = Join-Path $env:APPDATA "VoxHerd"
$logsDir = Join-Path $appDataDir "logs"
$hooksDir = Join-Path $appDataDir "hooks"

New-Item -ItemType Directory -Force -Path $appDataDir | Out-Null
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
New-Item -ItemType Directory -Force -Path $hooksDir | Out-Null

Write-Host "  Config: $appDataDir"
Write-Host "  Logs:   $logsDir"
Write-Host "  Hooks:  $hooksDir"

# -----------------------------------------------------------------------
# 6. Install hooks
# -----------------------------------------------------------------------
Write-Host "Installing hooks..." -ForegroundColor Yellow

$installHooks = Join-Path $RepoRoot "hooks\install-hooks.ps1"
if (Test-Path $installHooks) {
    & powershell -ExecutionPolicy Bypass -File $installHooks
} else {
    # Fallback: copy hook files manually
    $hookFiles = @(
        "on-stop.py", "on-stop.ps1", "on-stop.sh",
        "on-session-start.ps1", "on-session-start.sh",
        "on-notification.ps1", "on-notification.sh"
    )
    foreach ($f in $hookFiles) {
        $src = Join-Path $RepoRoot "hooks\$f"
        if (Test-Path $src) {
            Copy-Item $src (Join-Path $hooksDir $f) -Force
            Write-Host "  Installed: $f"
        }
    }
    Write-Host "  NOTE: Could not find install-hooks.ps1 — hooks copied but settings.json not updated" -ForegroundColor Yellow
}

# -----------------------------------------------------------------------
# 7. Write config with project dir
# -----------------------------------------------------------------------
$configFile = Join-Path $appDataDir "config.json"
$config = @{
    project_dir = $RepoRoot
    port = 7777
}
$config | ConvertTo-Json | Out-File -FilePath $configFile -Encoding utf8
Write-Host "  Config saved: $configFile"

# -----------------------------------------------------------------------
# 8. Verify
# -----------------------------------------------------------------------
Write-Host ""
Write-Host "Verifying installation..." -ForegroundColor Yellow

$bridgeMain = Join-Path $RepoRoot "bridge\__main__.py"
if (Test-Path $bridgeMain) {
    Write-Host "  Bridge server: OK" -ForegroundColor Green
} else {
    Write-Host "  Bridge server: NOT FOUND" -ForegroundColor Red
}

$trayMain = Join-Path $RepoRoot "windows\voxherd_tray\__main__.py"
if (Test-Path $trayMain) {
    Write-Host "  Tray app: OK" -ForegroundColor Green
} else {
    Write-Host "  Tray app: NOT FOUND" -ForegroundColor Red
}

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
Write-Host ""
Write-Host "=== Setup Complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "To start the bridge (terminal mode):"
Write-Host "  cd $RepoRoot"
Write-Host "  $venvDir\Scripts\activate"
Write-Host "  python -m bridge run --tts"
Write-Host ""
Write-Host "To start the tray app:"
Write-Host "  cd $RepoRoot"
Write-Host "  $venvDir\Scripts\activate"
Write-Host "  python -m windows.voxherd_tray"
Write-Host ""
Write-Host "To show QR code for iOS pairing:"
Write-Host "  python -m bridge qr"
Write-Host ""
