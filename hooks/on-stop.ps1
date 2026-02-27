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

$SessionId = if ($hookInput.session_id) { $hookInput.session_id } else { "" }
$Cwd = if ($hookInput.cwd) { $hookInput.cwd } else { "" }
$TranscriptPath = if ($hookInput.transcript_path) { $hookInput.transcript_path } else { "" }
$StopReason = if ($hookInput.stop_reason) { $hookInput.stop_reason } else { "completed" }

$ProjectDir = $Cwd
$ProjectName = if ($ProjectDir) { Split-Path -Leaf $ProjectDir } else { "unknown" }
$Assistant = if ($env:VOXHERD_HOOK_ASSISTANT) { $env:VOXHERD_HOOK_ASSISTANT.ToLower() } else { "claude" }

Write-DebugLog "=== on-stop.ps1 invoked ==="
Write-DebugLog "  SESSION_ID=$SessionId"
Write-DebugLog "  CWD=$Cwd"
Write-DebugLog "  PROJECT_NAME=$ProjectName"
Write-DebugLog "  TRANSCRIPT_PATH=$TranscriptPath"
Write-DebugLog "  TRANSCRIPT_EXISTS=$(Test-Path $TranscriptPath -ErrorAction SilentlyContinue)"
Write-DebugLog "  ASSISTANT=$Assistant"

# ---------------------------------------------------------------------------
# Generate summary via Haiku
# ---------------------------------------------------------------------------

$Summary = ""
if ($TranscriptPath -and (Test-Path $TranscriptPath -ErrorAction SilentlyContinue)) {
    Write-DebugLog "  Transcript exists, attempting Haiku summary..."
    try {
        # Get last 50 lines of transcript
        $transcriptTail = Get-Content -Path $TranscriptPath -Tail 50 -ErrorAction Stop | Out-String

        # Check if claude CLI is available
        $claudePath = Get-Command "claude" -ErrorAction SilentlyContinue
        if ($claudePath) {
            Write-DebugLog "  Calling Haiku for summary..."
            $prompt = "Summarize what was just accomplished in 1-2 sentences for a voice announcement. Be concise."

            # Create a temp file for piping transcript content
            $tempFile = [System.IO.Path]::GetTempFileName()
            try {
                Set-Content -Path $tempFile -Value $transcriptTail -Encoding UTF8
                $env:VOXHERD_HOOK_RUNNING = "1"
                $process = Start-Process -FilePath "claude" `
                    -ArgumentList "-p", "`"$prompt`"", "--model", "claude-haiku-4-5-20251001" `
                    -RedirectStandardInput $tempFile `
                    -RedirectStandardOutput "$tempFile.out" `
                    -RedirectStandardError "$tempFile.err" `
                    -NoNewWindow -PassThru

                # Wait with timeout (15 seconds) -- do NOT use -Wait on Start-Process
                # as it blocks indefinitely and defeats the timeout
                if (-not $process.WaitForExit(15000)) {
                    try { $process.Kill() } catch {}
                    Write-DebugLog "  Haiku timed out"
                } elseif ($process.ExitCode -eq 0) {
                    $Summary = (Get-Content "$tempFile.out" -Raw -ErrorAction SilentlyContinue).Trim()
                    Write-DebugLog "  Haiku exit code: 0"
                    Write-DebugLog "  SUMMARY=$Summary"
                } else {
                    $stderr = Get-Content "$tempFile.err" -Raw -ErrorAction SilentlyContinue
                    Write-DebugLog "  Haiku exit code: $($process.ExitCode), stderr: $stderr"
                }
            } finally {
                Remove-Item $tempFile -Force -ErrorAction SilentlyContinue
                Remove-Item "$tempFile.out" -Force -ErrorAction SilentlyContinue
                Remove-Item "$tempFile.err" -Force -ErrorAction SilentlyContinue
            }
        } else {
            Write-DebugLog "  Claude CLI not found, skipping Haiku summarization"
        }
    } catch {
        Write-DebugLog "  Haiku error: $_"
    }
} else {
    Write-DebugLog "  SKIPPED: transcript_path empty or file not found"
}

# Fallback if claude failed or returned empty
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
