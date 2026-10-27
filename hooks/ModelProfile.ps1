# ModelProfile.ps1 — resolve the install-time model profile for PowerShell hooks.
#
# Dot-source this, do not run it:
#
#   . (Join-Path $PSScriptRoot "ModelProfile.ps1")
#   $mp = Resolve-VoxHerdModelProfile     # not $profile -- that is an automatic variable
#   $argv = Get-VoxHerdCliArgv -Cli $mp.SummaryCli -Model $mp.SummaryModel -Prompt $p
#
# Resolution order, highest first: process environment, then models.env
# (%APPDATA%\VoxHerd\models.env, falling back to ~\.voxherd\models.env), then
# the hardcoded Claude Haiku defaults.
#
# This is the PowerShell twin of bridge/model_profile.py and
# hooks/model-profile.sh — the same rules in three languages, and it runs with
# no POSIX shell anywhere in sight. Change one, change the others.
#
# Three things matter here:
#   1. Nothing throws. A missing, unreadable or corrupt models.env degrades to
#      Claude Haiku. A hook must not block the assistant.
#   2. models.env is parsed line by line — never Invoke-Expression'd.
#   3. The CLI name is matched against an allowlist, never interpolated.

$script:VoxHerdDefaultCli = "claude"
$script:VoxHerdDefaultModel = "claude-haiku-4-5-20251001"
$script:VoxHerdAllowedClis = @("claude", "gemini", "codex", "agy")

function Test-VoxHerdCli {
    param([string]$Value)
    if (-not $Value) { return $false }
    return $script:VoxHerdAllowedClis -contains $Value.Trim().ToLower()
}

function Test-VoxHerdModel {
    # Model ids are letters, digits and . _ - : only — narrow on purpose, so
    # nothing surprising can reach an argument list we build.
    param([string]$Value)
    if (-not $Value) { return $false }
    $v = $Value.Trim()
    if ($v.Length -eq 0 -or $v.Length -gt 128) { return $false }
    return $v -cmatch '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
}

function Get-VoxHerdModelsEnvPath {
    # %APPDATA%\VoxHerd is where the Windows installer puts everything, but a
    # profile written by the POSIX installer (WSL, a shared home) lands in
    # ~\.voxherd. Prefer whichever actually exists; otherwise name the canonical
    # Windows location.
    if ($env:VOXHERD_MODELS_ENV) { return $env:VOXHERD_MODELS_ENV }

    $appdataPath = $null
    if ($env:APPDATA) {
        $appdataPath = Join-Path (Join-Path $env:APPDATA "VoxHerd") "models.env"
        if (Test-Path $appdataPath -PathType Leaf) { return $appdataPath }
    }

    $userHome = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
    if ($userHome) {
        $dotPath = Join-Path (Join-Path $userHome ".voxherd") "models.env"
        if (Test-Path $dotPath -PathType Leaf) { return $dotPath }
    }

    if ($appdataPath) { return $appdataPath }
    if ($userHome) { return (Join-Path (Join-Path $userHome ".voxherd") "models.env") }
    return "models.env"
}

function Read-VoxHerdModelsEnv {
    # Returns a hashtable of recognised, *valid* keys. Any failure at all —
    # missing file, permissions, binary garbage — returns an empty hashtable,
    # which means "use the defaults".
    param([string]$Path)

    $result = @{}
    try {
        if (-not $Path) { $Path = Get-VoxHerdModelsEnvPath }
        if (-not (Test-Path $Path -PathType Leaf)) { return $result }
        $lines = Get-Content -Path $Path -ErrorAction Stop
    } catch {
        return $result
    }

    foreach ($raw in $lines) {
        try {
            $line = "$raw".Trim()
            if (-not $line) { continue }
            if ($line.StartsWith("#")) { continue }
            if ($line.StartsWith("export ")) { $line = $line.Substring(7).Trim() }
            $eq = $line.IndexOf("=")
            if ($eq -lt 1) { continue }
            $key = $line.Substring(0, $eq).Trim()
            $value = $line.Substring($eq + 1).Trim()
            # Strip one layer of matching quotes, the way an .env file is written.
            if ($value.Length -ge 2) {
                $first = $value[0]
                $last = $value[$value.Length - 1]
                if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                    $value = $value.Substring(1, $value.Length - 2).Trim()
                }
            }
            if (-not $value) { continue }

            switch ($key) {
                "VOXHERD_SUMMARY_CLI" {
                    if (Test-VoxHerdCli $value) { $result[$key] = $value.Trim().ToLower() }
                }
                "VOXHERD_NLU_CLI" {
                    if (Test-VoxHerdCli $value) { $result[$key] = $value.Trim().ToLower() }
                }
                "VOXHERD_SUMMARY_MODEL" {
                    if (Test-VoxHerdModel $value) { $result[$key] = $value.Trim() }
                }
                "VOXHERD_NLU_MODEL" {
                    if (Test-VoxHerdModel $value) { $result[$key] = $value.Trim() }
                }
            }
        } catch {
            continue
        }
    }
    return $result
}

function Resolve-VoxHerdModelProfile {
    # Process environment beats models.env beats the Claude Haiku defaults.
    param([string]$Path)

    $fileValues = @{}
    try { $fileValues = Read-VoxHerdModelsEnv -Path $Path } catch { $fileValues = @{} }

    function Select-VoxHerdValue {
        param([hashtable]$FileValues, [string]$EnvKey, [string]$EnvValue, [bool]$IsCli)

        $envOk = if ($IsCli) { Test-VoxHerdCli $EnvValue } else { Test-VoxHerdModel $EnvValue }
        if ($envOk) {
            $v = if ($IsCli) { $EnvValue.Trim().ToLower() } else { $EnvValue.Trim() }
            return @{ Value = $v; Source = "env" }
        }
        if ($FileValues.ContainsKey($EnvKey)) {
            return @{ Value = $FileValues[$EnvKey]; Source = "file" }
        }
        $fallback = if ($IsCli) { $script:VoxHerdDefaultCli } else { $script:VoxHerdDefaultModel }
        return @{ Value = $fallback; Source = "default" }
    }

    $summaryCli   = Select-VoxHerdValue $fileValues "VOXHERD_SUMMARY_CLI"   $env:VOXHERD_SUMMARY_CLI   $true
    $summaryModel = Select-VoxHerdValue $fileValues "VOXHERD_SUMMARY_MODEL" $env:VOXHERD_SUMMARY_MODEL $false
    $nluCli       = Select-VoxHerdValue $fileValues "VOXHERD_NLU_CLI"       $env:VOXHERD_NLU_CLI       $true
    $nluModel     = Select-VoxHerdValue $fileValues "VOXHERD_NLU_MODEL"     $env:VOXHERD_NLU_MODEL     $false

    return [PSCustomObject]@{
        SummaryCli       = $summaryCli.Value
        SummaryModel     = $summaryModel.Value
        SummaryCliSource = $summaryCli.Source
        NluCli           = $nluCli.Value
        NluModel         = $nluModel.Value
        NluCliSource     = $nluCli.Source
    }
}

function Get-VoxHerdModelFlag {
    # claude and agy spell it --model; gemini and codex spell it -m. Getting this
    # wrong silently falls back to the CLI's own default model.
    param([string]$Cli)
    switch ("$Cli".Trim().ToLower()) {
        "gemini" { return "-m" }
        "codex"  { return "-m" }
        default  { return "--model" }
    }
}

function Get-VoxHerdCliArgv {
    # Build the argument list for a one-shot, non-interactive prompt. Returns a
    # string[] whose first element is the executable name.
    param(
        [string]$Cli,
        [string]$Model,
        [string]$Prompt,
        [switch]$JsonOutput
    )

    if (-not (Test-VoxHerdCli $Cli)) { $Cli = $script:VoxHerdDefaultCli }
    $Cli = $Cli.Trim().ToLower()
    if (-not (Test-VoxHerdModel $Model)) { $Model = $script:VoxHerdDefaultModel }
    $Model = $Model.Trim()
    $flag = Get-VoxHerdModelFlag -Cli $Cli

    if ($Cli -eq "codex") {
        # `codex exec` takes the prompt positionally, after the flags. It has no
        # single-envelope JSON mode, so -JsonOutput is a no-op there.
        return @("codex", "exec", $flag, $Model, $Prompt)
    }

    $argv = @($Cli, "-p", $Prompt, $flag, $Model)
    # agy is an agent first: a prompt starting with "/" would run as one of its
    # slash commands or skills.
    if ($Cli -eq "agy") { $argv += @("--disable-slash-commands") }
    if ($JsonOutput) {
        if ($Cli -eq "claude" -or $Cli -eq "agy") { $argv += @("--output-format", "json") }
        elseif ($Cli -eq "gemini") { $argv += @("-o", "json") }
    }
    return $argv
}
