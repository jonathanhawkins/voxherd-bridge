# build-windows-package.ps1 — Build VoxHerd Bridge + Tray App for Windows distribution
#
# Produces a PyInstaller .exe bundle and optionally an Inno Setup installer.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\build-windows-package.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\build-windows-package.ps1 -Installer
#
# Prerequisites:
#   pip install pyinstaller
#   (optional) Inno Setup 6 installed for -Installer flag

param(
    [switch]$Installer,
    [string]$OutputDir = "dist"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $PSScriptRoot }

Write-Host "=== VoxHerd Windows Build ===" -ForegroundColor Cyan
Write-Host "Repo root: $RepoRoot"

# Verify prerequisites
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "ERROR: Python not found in PATH" -ForegroundColor Red
    exit 1
}

$pyinstaller = Get-Command pyinstaller -ErrorAction SilentlyContinue
if (-not $pyinstaller) {
    Write-Host "PyInstaller not found. Installing..." -ForegroundColor Yellow
    pip install pyinstaller
}

# Install tray app dependencies
Write-Host "`nInstalling dependencies..." -ForegroundColor Yellow
pip install -r "$RepoRoot\windows\requirements-windows.txt"
pip install -r "$RepoRoot\bridge\requirements.txt"

# Create output directory
$distDir = Join-Path $RepoRoot $OutputDir
New-Item -ItemType Directory -Force -Path $distDir | Out-Null

# Build the tray app with PyInstaller
Write-Host "`nBuilding VoxHerd Tray App..." -ForegroundColor Yellow

$specContent = @"
# -*- mode: python ; coding: utf-8 -*-
import os, sys

repo_root = os.path.dirname(os.path.dirname(SPECPATH))

a = Analysis(
    [os.path.join(repo_root, 'windows', 'voxherd_tray', '__main__.py')],
    pathex=[repo_root],
    datas=[
        (os.path.join(repo_root, 'bridge'), 'bridge'),
        (os.path.join(repo_root, 'hooks'), 'hooks'),
    ],
    hiddenimports=[
        'bridge', 'bridge.cli', 'bridge.bridge_server', 'bridge.server_state',
        'bridge.session_manager', 'bridge.env_utils', 'bridge.tailscale',
        'bridge.auth', 'bridge.win_tts', 'bridge.narration',
        'pystray', 'pystray._win32', 'PIL', 'qrcode', 'pyttsx3',
        'uvicorn', 'fastapi', 'starlette',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='VoxHerdBridge',
    debug=False,
    strip=False,
    upx=True,
    console=False,
    icon=None,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False,
    upx=True,
    name='VoxHerdBridge',
)
"@

$specPath = Join-Path $distDir "VoxHerdBridge.spec"
$specContent | Out-File -FilePath $specPath -Encoding utf8

Push-Location $RepoRoot
try {
    pyinstaller --distpath "$distDir" --workpath "$distDir\build" --clean --noconfirm $specPath
} finally {
    Pop-Location
}

$bundleDir = Join-Path $distDir "VoxHerdBridge"
if (Test-Path $bundleDir) {
    Write-Host "`nBuild successful!" -ForegroundColor Green
    Write-Host "Output: $bundleDir\VoxHerdBridge.exe"

    # Calculate size
    $size = (Get-ChildItem -Recurse $bundleDir | Measure-Object -Property Length -Sum).Sum
    $sizeMB = [math]::Round($size / 1MB, 1)
    Write-Host "Bundle size: ${sizeMB}MB"
} else {
    Write-Host "ERROR: Build failed — output directory not found" -ForegroundColor Red
    exit 1
}

# Optionally build Inno Setup installer
if ($Installer) {
    $iscc = Get-Command iscc -ErrorAction SilentlyContinue
    if (-not $iscc) {
        # Try default install location
        $isccPath = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
        if (Test-Path $isccPath) {
            $iscc = Get-Item $isccPath
        } else {
            Write-Host "WARNING: Inno Setup not found. Skipping installer creation." -ForegroundColor Yellow
            Write-Host "Install from: https://jrsoftware.org/isdownload.php" -ForegroundColor Yellow
            exit 0
        }
    }

    Write-Host "`nBuilding installer with Inno Setup..." -ForegroundColor Yellow

    $issContent = @"
[Setup]
AppName=VoxHerd Bridge
AppVersion=1.0.0
AppPublisher=VoxHerd
DefaultDirName={autopf}\VoxHerd
DefaultGroupName=VoxHerd
OutputDir=$distDir
OutputBaseFilename=VoxHerdBridge-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest

[Files]
Source: "$bundleDir\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{group}\VoxHerd Bridge"; Filename: "{app}\VoxHerdBridge.exe"
Name: "{autodesktop}\VoxHerd Bridge"; Filename: "{app}\VoxHerdBridge.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\VoxHerdBridge.exe"; Description: "Launch VoxHerd Bridge"; Flags: nowait postinstall skipifsilent
"@

    $issPath = Join-Path $distDir "VoxHerdBridge.iss"
    $issContent | Out-File -FilePath $issPath -Encoding utf8

    & $iscc.FullName $issPath

    $installerPath = Join-Path $distDir "VoxHerdBridge-Setup.exe"
    if (Test-Path $installerPath) {
        Write-Host "`nInstaller built!" -ForegroundColor Green
        Write-Host "Output: $installerPath"
    }
}

Write-Host "`n=== Done ===" -ForegroundColor Cyan
