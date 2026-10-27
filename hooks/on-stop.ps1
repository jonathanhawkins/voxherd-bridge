# VoxHerd on-stop hook (PowerShell)
# Runs when an assistant session ends a turn. Generates a summary and notifies the bridge server.
# Must NEVER block Claude Code -- all external calls have timeouts, errors are swallowed.

# Prevent recursive hook execution when we spawn `claude -p` for summaries
if ($env:VOXHERD_HOOK_RUNNING) {
    exit 0
}
$env:VOXHERD_HOOK_RUNNING = "1"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

$BridgeUrl = "http://localhost:7777/api/events"
$ConfigDir = Join-Path $env:APPDATA "VoxHerd"
# $PSScriptRoot is set however the script was invoked (-File, & 'path', dot-source);
# $MyInvocation is the PS2 fallback.
$HookDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$LogDir = Join-Path $ConfigDir "logs"
$DebugLog = Join-Path $LogDir "on-stop-debug.log"
$ErrorLog = Join-Path $LogDir "hook-errors.log"
$TokenFile = Join-Path $ConfigDir "auth_token"

# Ensure directories exist
try {
    if (-not (Test-Path $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    }
} catch {}

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

function Write-DebugLog {
    param([string]$Message)
    try {
        $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Add-Content -Path $DebugLog -Value "[$timestamp] $Message" -ErrorAction SilentlyContinue
    } catch {}
}

function Write-ErrorLog {
    param([string]$Message)
    try {
        $timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        Add-Content -Path $ErrorLog -Value "[$timestamp] on-stop.ps1: $Message" -ErrorAction SilentlyContinue
    } catch {}
}

# ---------------------------------------------------------------------------
# Model profile — which small model summarises this turn
# ---------------------------------------------------------------------------
#
# Resolved at install time from models.env, not per request. If ModelProfile.ps1
# is missing for any reason, fall back to the hardcoded Claude Haiku default —
# a hook must never block the assistant over a missing helper.

$ModelProfile = $null
try {
    $modelProfilePath = Join-Path $HookDir "ModelProfile.ps1"
    if (Test-Path $modelProfilePath -PathType Leaf) {
        . $modelProfilePath
        $ModelProfile = Resolve-VoxHerdModelProfile
    }
} catch {
    $ModelProfile = $null
}
if (-not $ModelProfile) {
    $ModelProfile = [PSCustomObject]@{
        SummaryCli       = "claude"
        SummaryModel     = "claude-haiku-4-5-20251001"
        SummaryCliSource = "default"
        NluCli           = "claude"
        NluModel         = "claude-haiku-4-5-20251001"
        NluCliSource     = "default"
    }
}

# Truncate debug log if over 500KB
try {
    if (Test-Path $DebugLog) {
        $logInfo = Get-Item $DebugLog
        if ($logInfo.Length -gt 500000) {
            $lines = Get-Content $DebugLog -Tail 500
            Set-Content -Path $DebugLog -Value $lines -ErrorAction SilentlyContinue
        }
    }
} catch {}

# ---------------------------------------------------------------------------
# Read stdin JSON
# ---------------------------------------------------------------------------

try {
    $rawInput = [Console]::In.ReadToEnd()
    $hookInput = $rawInput | ConvertFrom-Json
} catch {
    Write-DebugLog "Failed to read stdin: $_"
    exit 0
}

$SessionId = if ($hookInput.session_id) { $hookInput.session_id } elseif ($hookInput.sessionId) { $hookInput.sessionId } else { "" }
$Cwd = if ($hookInput.cwd) { $hookInput.cwd } elseif ($hookInput.workspaceRoot) { $hookInput.workspaceRoot } else { "" }
$TranscriptPath = if ($hookInput.transcript_path) { $hookInput.transcript_path } elseif ($hookInput.transcriptPath) { $hookInput.transcriptPath } else { "" }
$StopReason = if ($hookInput.stop_reason) { $hookInput.stop_reason } elseif ($hookInput.stopReason) { $hookInput.stopReason } else { "completed" }

$ProjectDir = $Cwd
$ProjectName = if ($ProjectDir) { Split-Path -Leaf $ProjectDir } else { "unknown" }
$Assistant = if ($env:VOXHERD_HOOK_ASSISTANT) { $env:VOXHERD_HOOK_ASSISTANT.ToLower() } else { "claude" }

# VOXHERD_QUIET: this session never speaks. Skip the (slow, paid) Haiku summary
# and tell the bridge to stay silent via skip_tts. The stop event is still
# POSTed so the session stays visible on the dashboard/lens.
$Quiet = $false
if ($env:VOXHERD_QUIET -and @("1","true","yes","on") -contains $env:VOXHERD_QUIET.ToLower()) {
    $Quiet = $true
}

Write-DebugLog "=== on-stop.ps1 invoked ==="
Write-DebugLog "  SESSION_ID=$SessionId"
Write-DebugLog "  CWD=$Cwd"
Write-DebugLog "  PROJECT_NAME=$ProjectName"
Write-DebugLog "  TRANSCRIPT_PATH=$TranscriptPath"
Write-DebugLog "  TRANSCRIPT_EXISTS=$(Test-Path $TranscriptPath -ErrorAction SilentlyContinue)"
Write-DebugLog "  ASSISTANT=$Assistant"

# ---------------------------------------------------------------------------
# Generate summary via the profile's model
# ---------------------------------------------------------------------------

$Summary = ""
if ($Quiet) {
    Write-DebugLog "  VOXHERD_QUIET set — skipping AI summary"
} elseif ($TranscriptPath -and (Test-Path $TranscriptPath -ErrorAction SilentlyContinue)) {
    Write-DebugLog "  Transcript exists, attempting summary..."
    try {
        # Get last 50 lines of transcript
        $transcriptTail = Get-Content -Path $TranscriptPath -Tail 50 -ErrorAction Stop | Out-String

        $prompt = "Summarize what was just accomplished in 1-2 sentences for a voice announcement. Be concise."

        # Build the command from the install profile. If ModelProfile.ps1 did
        # not load, fall back to the hardcoded Claude Haiku default.
        if (Get-Command "Get-VoxHerdCliArgv" -ErrorAction SilentlyContinue) {
            $argv = Get-VoxHerdCliArgv -Cli $ModelProfile.SummaryCli `
                                       -Model $ModelProfile.SummaryModel `
                                       -Prompt $prompt
        } else {
            $argv = @("claude", "-p", $prompt, "--model", "claude-haiku-4-5-20251001")
        }
        $summaryExe = $argv[0]
        $summaryArgs = @()
        for ($i = 1; $i -lt $argv.Count; $i++) {
            $a = "$($argv[$i])"
            if ($a -match '\s') { $summaryArgs += "`"$a`"" } else { $summaryArgs += $a }
        }

        # Check if the summary CLI is available
        $summaryCliPath = Get-Command $summaryExe -ErrorAction SilentlyContinue
        if ($summaryCliPath) {
            Write-DebugLog "  Calling $summaryExe ($($ModelProfile.SummaryModel)) for summary..."

            # Create a temp file for piping transcript content
            $tempFile = [System.IO.Path]::GetTempFileName()
            try {
                Set-Content -Path $tempFile -Value $transcriptTail -Encoding UTF8
                $env:VOXHERD_HOOK_RUNNING = "1"
                $process = Start-Process -FilePath $summaryExe `
                    -ArgumentList $summaryArgs `
                    -RedirectStandardInput $tempFile `
                    -RedirectStandardOutput "$tempFile.out" `
                    -RedirectStandardError "$tempFile.err" `
                    -NoNewWindow -PassThru

                # Wait with timeout (15 seconds) -- do NOT use -Wait on Start-Process
                # as it blocks indefinitely and defeats the timeout
                if (-not $process.WaitForExit(15000)) {
                    try { $process.Kill() } catch {}
                    Write-DebugLog "  $summaryExe timed out"
                } elseif ($process.ExitCode -eq 0) {
                    $Summary = (Get-Content "$tempFile.out" -Raw -ErrorAction SilentlyContinue).Trim()
                    Write-DebugLog "  $summaryExe exit code: 0"
                    Write-DebugLog "  SUMMARY=$Summary"
                } else {
                    $stderr = Get-Content "$tempFile.err" -Raw -ErrorAction SilentlyContinue
                    Write-DebugLog "  $summaryExe exit code: $($process.ExitCode), stderr: $stderr"
                }
            } finally {
                Remove-Item $tempFile -Force -ErrorAction SilentlyContinue
                Remove-Item "$tempFile.out" -Force -ErrorAction SilentlyContinue
                Remove-Item "$tempFile.err" -Force -ErrorAction SilentlyContinue
            }
        } else {
            Write-DebugLog "  $summaryExe CLI not found, skipping CLI summarization"
        }
    } catch {
        Write-DebugLog "  Summary error: $_"
    }
} else {
    Write-DebugLog "  SKIPPED: transcript_path empty or file not found"
}

# Fallback if the summary CLI failed or returned empty
if (-not $Summary) {
    $Summary = "Task completed."
    Write-DebugLog "  Using fallback summary"
}

# ---------------------------------------------------------------------------
# POST to bridge server
# ---------------------------------------------------------------------------

$Timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

$payload = @{
    event           = "stop"
    session_id      = $SessionId
    project         = $ProjectName
    project_dir     = $ProjectDir
    assistant       = $Assistant
    summary         = $Summary
    stop_reason     = $StopReason
    transcript_path = $TranscriptPath
    timestamp       = $Timestamp
    skip_tts        = $Quiet
} | ConvertTo-Json -Compress

# Read auth token
$headers = @{
    "Content-Type" = "application/json"
    "X-VoxHerd"    = "1"
}

try {
    if (Test-Path $TokenFile) {
        $authToken = (Get-Content $TokenFile -Raw -ErrorAction SilentlyContinue).Trim()
        if ($authToken) {
            $headers["Authorization"] = "Bearer $authToken"
        }
    }
} catch {}

try {
    $response = Invoke-RestMethod -Uri $BridgeUrl `
        -Method Post `
        -Headers $headers `
        -Body $payload `
        -TimeoutSec 5 `
        -ErrorAction Stop
    Write-DebugLog "  Bridge POST succeeded"
} catch {
    Write-ErrorLog "POST failed: $_"
}
