# VoxHerd on-session-start hook (PowerShell)
# Registers a new session with the bridge server.
# Must be fast -- no AI calls here. Must NEVER block Claude Code.

# Prevent recursive hook execution
if ($env:VOXHERD_HOOK_RUNNING) {
    exit 0
}
$env:VOXHERD_HOOK_RUNNING = "1"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

$BridgeUrl = "http://localhost:7777/api/sessions/register"
$ConfigDir = Join-Path $env:APPDATA "VoxHerd"
$LogDir = Join-Path $ConfigDir "logs"
$ErrorLog = Join-Path $LogDir "hook-errors.log"
$TokenFile = Join-Path $ConfigDir "auth_token"

# Ensure directories exist
try {
    if (-not (Test-Path $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    }
} catch {}

# ---------------------------------------------------------------------------
# Read stdin JSON
# ---------------------------------------------------------------------------

try {
    $rawInput = [Console]::In.ReadToEnd()
    $hookInput = $rawInput | ConvertFrom-Json
} catch {
    exit 0
}

$SessionId = if ($hookInput.session_id) { $hookInput.session_id } else { "" }
$Cwd = if ($hookInput.cwd) { $hookInput.cwd } else { "" }

$ProjectDir = $Cwd
$ProjectName = if ($ProjectDir) { Split-Path -Leaf $ProjectDir } else { "unknown" }
$Assistant = if ($env:VOXHERD_HOOK_ASSISTANT) { $env:VOXHERD_HOOK_ASSISTANT.ToLower() } else { "claude" }
$Timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

# ---------------------------------------------------------------------------
# Build payload
# ---------------------------------------------------------------------------

$payload = @{
    session_id  = $SessionId
    project     = $ProjectName
    project_dir = $ProjectDir
    assistant   = $Assistant
    status      = "active"
    timestamp   = $Timestamp
} | ConvertTo-Json -Compress

# ---------------------------------------------------------------------------
# POST to bridge server
# ---------------------------------------------------------------------------

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
} catch {
    try {
        $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        Add-Content -Path $ErrorLog -Value "[$ts] on-session-start: POST failed: $_" -ErrorAction SilentlyContinue
    } catch {}
}
