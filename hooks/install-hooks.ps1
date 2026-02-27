# install-hooks.ps1 - Install VoxHerd hooks for supported assistant CLIs on Windows.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File hooks\install-hooks.ps1
#   $env:HOOK_AGENTS="claude,gemini"; powershell -ExecutionPolicy Bypass -File hooks\install-hooks.ps1
#
# Notes:
# - Claude + Gemini support lifecycle hooks and are configured here.
# - Codex currently has no native lifecycle hooks; dispatch is supported by the bridge.

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Locate script and hook files
# ---------------------------------------------------------------------------

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$HookScripts = @(
    "on-stop.py"
    "on-stop.ps1"
    "on-stop.sh"
    "on-session-start.ps1"
    "on-session-start.sh"
    "on-notification.ps1"
    "on-notification.sh"
    "on-subagent-start.sh"
    "on-subagent-stop.sh"
)

$VoxHerdDir = Join-Path $env:APPDATA "VoxHerd"
$HooksDest = Join-Path $VoxHerdDir "hooks"
$LogsDir = Join-Path $VoxHerdDir "logs"

# Parse target agents from environment
$RawAgents = if ($env:HOOK_AGENTS) { $env:HOOK_AGENTS } else { "claude" }
$TargetAgents = ($RawAgents -split ",") | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ } | Select-Object -Unique
if (-not $TargetAgents) {
    $TargetAgents = @("claude")
}

# ---------------------------------------------------------------------------
# Create directories and copy hook scripts
# ---------------------------------------------------------------------------

Write-Host "Creating directories..."
New-Item -ItemType Directory -Path $HooksDest -Force | Out-Null
New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null

Write-Host "Copying hook scripts to $HooksDest..."
foreach ($script in $HookScripts) {
    $src = Join-Path $ScriptDir $script
    if (-not (Test-Path $src)) {
        # Not all scripts are required (e.g., .sh scripts are optional on Windows)
        Write-Host "  Skipped (not found): $script"
        continue
    }
    Copy-Item -Path $src -Destination (Join-Path $HooksDest $script) -Force
    Write-Host "  Installed: $script"
}

# ---------------------------------------------------------------------------
# Helper functions for settings.json manipulation
# ---------------------------------------------------------------------------

function Read-Settings {
    param([string]$Path)

    $dir = Split-Path -Parent $Path
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }

    if (-not (Test-Path $Path)) {
        Set-Content -Path $Path -Value "{}" -Encoding UTF8
    }

    $content = Get-Content -Path $Path -Raw -Encoding UTF8
    try {
        $null = $content | ConvertFrom-Json
    } catch {
        Write-Error "Error: $Path contains invalid JSON. Please fix it and re-run."
        exit 1
    }
    return $content
}

function Write-Settings {
    param([string]$Path, [string]$Content)

    # Validate JSON before writing
    try {
        $obj = $Content | ConvertFrom-Json
        $formatted = $obj | ConvertTo-Json -Depth 20
        Set-Content -Path $Path -Value $formatted -Encoding UTF8
    } catch {
        Write-Error "Error: failed to write updated settings: $Path"
        exit 1
    }
}

function Merge-Hook {
    param(
        [PSCustomObject]$Settings,
        [string]$HookType,
        [PSCustomObject]$NewEntry,
        [string]$Marker
    )

    # Ensure .hooks exists
    if (-not $Settings.PSObject.Properties["hooks"]) {
        $Settings | Add-Member -NotePropertyName "hooks" -NotePropertyValue ([PSCustomObject]@{})
    }

    $hooks = $Settings.hooks

    # Check if the hook type key exists
    if (-not $hooks.PSObject.Properties[$HookType]) {
        $hooks | Add-Member -NotePropertyName $HookType -NotePropertyValue @($NewEntry)
        return $Settings
    }

    # Check if our hook is already present (by marker in command string)
    $existing = $hooks.$HookType
    $alreadyPresent = $false
    foreach ($entry in $existing) {
        if ($entry.hooks) {
            foreach ($h in $entry.hooks) {
                if ($h.command -and $h.command.Contains($Marker)) {
                    $alreadyPresent = $true
                    break
                }
            }
        }
        if ($alreadyPresent) { break }
    }

    if (-not $alreadyPresent) {
        $hooks.$HookType = @($existing) + @($NewEntry)
    }

    return $Settings
}

# ---------------------------------------------------------------------------
# Install hooks for each target agent
# ---------------------------------------------------------------------------

$HooksPath = $HooksDest -replace '\\', '/'
# Also prepare escaped path for JSON embedding
$HooksPathEscaped = $HooksDest -replace '\\', '\\\\'

$Installed = @()
$Skipped = @()

foreach ($agent in $TargetAgents) {
    switch ($agent) {
        "claude" {
            Write-Host "Updating Claude settings..."
            $settingsPath = Join-Path $env:USERPROFILE ".claude\settings.json"
            $content = Read-Settings -Path $settingsPath
            $settings = $content | ConvertFrom-Json

            # Remove old flat hook keys if present
            foreach ($key in @("hooks.Stop", "hooks.Notification", "hooks.SessionStart", "hooks.SubagentStart", "hooks.SubagentStop")) {
                if ($settings.PSObject.Properties[$key]) {
                    $settings.PSObject.Properties.Remove($key)
                }
            }

            # On Windows, use PowerShell for .ps1 hooks and python3 for .py hooks
            $stopCmd = "powershell -ExecutionPolicy Bypass -File `"$HooksDest\on-stop.ps1`""
            $stopPyCmd = "python3 `"$HooksDest\on-stop.py`""
            $notifCmd = "powershell -ExecutionPolicy Bypass -File `"$HooksDest\on-notification.ps1`""
            $sessCmd = "powershell -ExecutionPolicy Bypass -File `"$HooksDest\on-session-start.ps1`""

            # Prefer Python on-stop (it has richer transcript parsing)
            # Fall back to PowerShell if python3 is not available
            $python3Available = Get-Command "python3" -ErrorAction SilentlyContinue
            $pythonAvailable = Get-Command "python" -ErrorAction SilentlyContinue
            if ($python3Available) {
                $stopEntry = [PSCustomObject]@{
                    matcher = ""
                    hooks = @([PSCustomObject]@{
                        type = "command"
                        command = "set VOXHERD_HOOK_ASSISTANT=claude&& python3 `"$HooksDest\on-stop.py`""
                    })
                }
            } elseif ($pythonAvailable) {
                $stopEntry = [PSCustomObject]@{
                    matcher = ""
                    hooks = @([PSCustomObject]@{
                        type = "command"
                        command = "set VOXHERD_HOOK_ASSISTANT=claude&& python `"$HooksDest\on-stop.py`""
                    })
                }
            } else {
                $stopEntry = [PSCustomObject]@{
                    matcher = ""
                    hooks = @([PSCustomObject]@{
                        type = "command"
                        command = "powershell -ExecutionPolicy Bypass -Command `"`$env:VOXHERD_HOOK_ASSISTANT='claude'; & '$HooksDest\on-stop.ps1'`""
                    })
                }
            }

            $notifEntry = [PSCustomObject]@{
                matcher = ""
                hooks = @([PSCustomObject]@{
                    type = "command"
                    command = "powershell -ExecutionPolicy Bypass -Command `"`$env:VOXHERD_HOOK_ASSISTANT='claude'; & '$HooksDest\on-notification.ps1'`""
                })
            }
            $sessEntry = [PSCustomObject]@{
                matcher = ""
                hooks = @([PSCustomObject]@{
                    type = "command"
                    command = "powershell -ExecutionPolicy Bypass -Command `"`$env:VOXHERD_HOOK_ASSISTANT='claude'; & '$HooksDest\on-session-start.ps1'`""
                })
            }

            $settings = Merge-Hook -Settings $settings -HookType "Stop" -NewEntry $stopEntry -Marker "on-stop."
            $settings = Merge-Hook -Settings $settings -HookType "Notification" -NewEntry $notifEntry -Marker "on-notification."
            $settings = Merge-Hook -Settings $settings -HookType "SessionStart" -NewEntry $sessEntry -Marker "on-session-start."

            Write-Settings -Path $settingsPath -Content ($settings | ConvertTo-Json -Depth 20)
            $Installed += "claude:$settingsPath"
        }
        "gemini" {
            Write-Host "Updating Gemini settings..."
            $settingsPath = Join-Path $env:USERPROFILE ".gemini\settings.json"
            $content = Read-Settings -Path $settingsPath
            $settings = $content | ConvertFrom-Json

            # Remove old flat hook keys if present
            foreach ($key in @("hooks.AfterAgent", "hooks.Notification", "hooks.SessionStart")) {
                if ($settings.PSObject.Properties[$key]) {
                    $settings.PSObject.Properties.Remove($key)
                }
            }

            # Gemini: AfterAgent maps to VoxHerd "stop"
            $python3Available = Get-Command "python3" -ErrorAction SilentlyContinue
            $pythonAvailable = Get-Command "python" -ErrorAction SilentlyContinue
            if ($python3Available) {
                $afterEntry = [PSCustomObject]@{
                    matcher = ""
                    hooks = @([PSCustomObject]@{
                        type = "command"
                        command = "set VOXHERD_HOOK_ASSISTANT=gemini&& python3 `"$HooksDest\on-stop.py`""
                    })
                }
            } elseif ($pythonAvailable) {
                $afterEntry = [PSCustomObject]@{
                    matcher = ""
                    hooks = @([PSCustomObject]@{
                        type = "command"
                        command = "set VOXHERD_HOOK_ASSISTANT=gemini&& python `"$HooksDest\on-stop.py`""
                    })
                }
            } else {
                $afterEntry = [PSCustomObject]@{
                    matcher = ""
                    hooks = @([PSCustomObject]@{
                        type = "command"
                        command = "powershell -ExecutionPolicy Bypass -Command `"`$env:VOXHERD_HOOK_ASSISTANT='gemini'; & '$HooksDest\on-stop.ps1'`""
                    })
                }
            }

            $notifEntry = [PSCustomObject]@{
                matcher = ""
                hooks = @([PSCustomObject]@{
                    type = "command"
                    command = "powershell -ExecutionPolicy Bypass -Command `"`$env:VOXHERD_HOOK_ASSISTANT='gemini'; & '$HooksDest\on-notification.ps1'`""
                })
            }
            $sessEntry = [PSCustomObject]@{
                matcher = ""
                hooks = @([PSCustomObject]@{
                    type = "command"
                    command = "powershell -ExecutionPolicy Bypass -Command `"`$env:VOXHERD_HOOK_ASSISTANT='gemini'; & '$HooksDest\on-session-start.ps1'`""
                })
            }

            $settings = Merge-Hook -Settings $settings -HookType "AfterAgent" -NewEntry $afterEntry -Marker "on-stop."
            $settings = Merge-Hook -Settings $settings -HookType "Notification" -NewEntry $notifEntry -Marker "on-notification."
            $settings = Merge-Hook -Settings $settings -HookType "SessionStart" -NewEntry $sessEntry -Marker "on-session-start."

            Write-Settings -Path $settingsPath -Content ($settings | ConvertTo-Json -Depth 20)
            $Installed += "gemini:$settingsPath"
        }
        "codex" {
            $Skipped += "codex (no native lifecycle hook API)"
        }
        default {
            $Skipped += "$agent (unsupported)"
        }
    }
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "VoxHerd hooks installed successfully."
Write-Host ""
Write-Host "  Hook scripts:  $HooksDest\"
foreach ($script in $HookScripts) {
    $dest = Join-Path $HooksDest $script
    if (Test-Path $dest) {
        Write-Host "    - $script"
    }
}
Write-Host "  Log directory: $LogsDir\"
Write-Host ""

if ($Installed.Count -gt 0) {
    Write-Host "Configured assistant settings:"
    foreach ($item in $Installed) {
        Write-Host "  - $item"
    }
}

if ($Skipped.Count -gt 0) {
    Write-Host ""
    Write-Host "Skipped:"
    foreach ($item in $Skipped) {
        Write-Host "  - $item"
    }
}

Write-Host ""
Write-Host "To uninstall, remove VoxHerd entries from the configured settings files"
Write-Host "and delete $VoxHerdDir\"
