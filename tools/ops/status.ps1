<#
.SYNOPSIS
  Is PPR running? (Wave 10A, operations) - prints PASS/FAIL per check; exit code 0 = all pass.

.DESCRIPTION
  Checks, without changing anything:
    * the scheduled tasks in \PPR\ exist, and their last result (0 = success);
    * the API answers http://127.0.0.1:<PPR_API_PORT, 8000>/api/health (database reachable);
    * the user interface answers http://127.0.0.1:<PPR_UI_PORT, 3000>/healthz?deep=1
      (the interface is up and reaches the API);
    * ppr.cli ops-status: schema version, least-privilege runtime account (production),
      last successful nightly PR synchronization within 26 hours; MOCK/DEMO data in the
      production database is shown as WARN (never a failure, D-40).
  -SkipTasks leaves out the Task Scheduler check (e.g. on a development machine).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\ops\status.ps1
#>
param(
    [string]$RepoRoot = "",
    [int]$ApiPort = 0,
    [int]$UiPort = 0,
    [switch]$SkipTasks
)
# Windows PowerShell 5.1 has no $PSScriptRoot inside param defaults: resolve here.
if (-not $RepoRoot) {
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
    $RepoRoot = (Resolve-Path (Join-Path $here "..\..")).Path
}
# Ports as the services read them: the environment, then the repository's .env, then defaults.
function Setting([string]$Name, [string]$Default) {
    $v = [Environment]::GetEnvironmentVariable($Name)
    if ($v) { return $v }
    $file = Join-Path $RepoRoot ".env"
    if (Test-Path $file) {
        foreach ($line in Get-Content $file -Encoding UTF8) {
            $t = $line.Trim()
            if ($t -and -not $t.StartsWith("#") -and $t.StartsWith("$Name=")) {
                return $t.Substring($Name.Length + 1).Trim().Trim('"').Trim("'")
            }
        }
    }
    return $Default
}
if (-not $ApiPort) { $ApiPort = [int](Setting "PPR_API_PORT" "8000") }
if (-not $UiPort) { $UiPort = [int](Setting "PPR_UI_PORT" "3000") }
$failed = 0
$script:warned = 0
function Report([bool]$Ok, [string]$What) {
    if ($Ok) { Write-Output "PASS  $What" } else { Write-Output "FAIL  $What"; $script:failed++ }
}

if (-not $SkipTasks) {
    foreach ($name in @("PPR API", "PPR UI", "PPR Nightly Sync", "PPR Backup")) {
        $task = Get-ScheduledTask -TaskPath "\PPR\" -TaskName $name -ErrorAction SilentlyContinue
        if (-not $task) { Report $false "task \PPR\$name is not registered"; continue }
        $info = $task | Get-ScheduledTaskInfo
        $ok = ($info.LastTaskResult -eq 0) -or ($task.State -eq "Running")
        Report $ok ("task \PPR\{0}: {1}, last run {2}, last result {3}" -f `
            $name, $task.State, $info.LastRunTime, $info.LastTaskResult)
    }
}

function Probe([string]$Url) {
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 10
        return $r.StatusCode
    } catch {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        return 0
    }
}
$api = Probe "http://127.0.0.1:$ApiPort/api/health"
Report ($api -eq 200) "API health http://127.0.0.1:$ApiPort/api/health -> $api"
$ui = Probe "http://127.0.0.1:$UiPort/healthz?deep=1"
Report ($ui -eq 200) "user interface http://127.0.0.1:$UiPort/healthz?deep=1 -> $ui"

$python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
Push-Location (Join-Path $RepoRoot "backend")
try {
    # 2>&1 turns stderr into error records: never let them stop this script.
    $ErrorActionPreference = "Continue"
    $opsOut = & $python -m ppr.cli ops-status 2>&1
    $opsCode = $LASTEXITCODE
    $opsOut | ForEach-Object { Write-Output "      $_" }
    Report ($opsCode -eq 0) "ppr.cli ops-status"
    # D-40: MOCK/DEMO data in production is a warning, never a failure - but always shown.
    $warnings = @($opsOut | Where-Object { "$_" -match '^WARN ' })
    foreach ($w in $warnings) { Write-Output ("WARN  " + ("$w" -replace '^WARN\s+', '')) }
    $script:warned = $warnings.Count
} finally { Pop-Location }

Write-Output ("logs: {0}" -f (Join-Path $RepoRoot "logs"))
if ($failed) { Write-Output "status: FAIL ($failed)"; exit 1 }
if ($script:warned) { Write-Output "status: PASS with $($script:warned) warning(s) - see WARN above"; exit 0 }
Write-Output "status: PASS"
exit 0
