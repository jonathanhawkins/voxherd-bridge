# VoxHerd on-notification hook (PowerShell)
# Forwards notification payloads to the bridge server.
# Must NEVER block Claude Code -- all external calls have timeouts, errors are swallowed.

# Prevent recursive hook execution
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
$ProjectName = if ($Cwd) { Split-Path -Leaf $Cwd } else { "unknown" }
$Assistant = if ($env:VOXHERD_HOOK_ASSISTANT) { $env:VOXHERD_HOOK_ASSISTANT.ToLower() } else { "claude" }
$Timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

# ---------------------------------------------------------------------------
# Build payload -- merge original input with VoxHerd fields
# ---------------------------------------------------------------------------

# Start with the original input as a hashtable, then add our fields
try {
    $payloadObj = @{}
    # Copy all properties from the original input
    $hookInput.PSObject.Properties | ForEach-Object {
        $payloadObj[$_.Name] = $_.Value
    }
    # Add/override VoxHerd-specific fields
    $payloadObj["event"] = "notification"
    $payloadObj["project"] = $ProjectName
    $payloadObj["project_dir"] = $Cwd
    $payloadObj["assistant"] = $Assistant
    $payloadObj["timestamp"] = $Timestamp

    $payload = $payloadObj | ConvertTo-Json -Compress
} catch {
    try {
        $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        Add-Content -Path $ErrorLog -Value "[$ts] on-notification: payload build failed: $_" -ErrorAction SilentlyContinue
    } catch {}
    exit 0
}

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
        Add-Content -Path $ErrorLog -Value "[$ts] on-notification: POST failed (session=$SessionId project=$ProjectName): $_" -ErrorAction SilentlyContinue
    } catch {}
}
