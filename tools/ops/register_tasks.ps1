<#
.SYNOPSIS
  Register the PPR scheduled tasks on the hospital's Windows Server (Wave 10A, operations).

.DESCRIPTION
  Creates four tasks in Task Scheduler folder \PPR\ (run as Administrator):

    PPR API           at system start-up (1 minute delay, after PostgreSQL) and every
                      5 minutes as a watchdog: python tools\ops\supervise.py api
    PPR UI            the same: python tools\ops\supervise.py ui
    PPR Nightly Sync  every day at -NightlyTime: python tools\ops\supervise.py nightly
    PPR Backup        every day at -BackupTime: python tools\ops\supervise.py backup
                      (dump + manifest into PPR_BACKUP_DIR; copying them off the server and
                      their retention follow the hospital's backup policy)

  The supervisor restarts a service that exits or stops answering its health check and keeps
  dated logs in <repository>\logs. The supervisor itself is started again by Task Scheduler
  after every reboot and, should it ever stop, by the 5-minute watchdog trigger (a running
  supervisor makes that trigger do nothing: "do not start a new instance"). "Restart on
  failure" (every minute) additionally covers a launch that fails.

  The tasks run as -RunAsUser (a Windows service account chosen by IT, with read access to the
  repository folder and write access to its logs and run folders). Its password is asked for
  and handed to Task Scheduler only; this script stores nothing.

  -PrintOnly builds the tasks and prints them without registering anything (no Administrator
  rights needed) - use it to review, and in verification runs.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\ops\register_tasks.ps1 -RunAsUser HOSP\svc_ppr -NightlyTime 01:30 -BackupTime 03:00
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\ops\register_tasks.ps1 -PrintOnly -NightlyTime 01:30 -BackupTime 03:00
#>
param(
    [string]$RepoRoot = "",
    [Parameter(Mandatory = $true)][ValidatePattern('^\d{2}:\d{2}$')][string]$NightlyTime,
    [Parameter(Mandatory = $true)][ValidatePattern('^\d{2}:\d{2}$')][string]$BackupTime,
    [string]$RunAsUser = "",
    [switch]$PrintOnly
)
# Windows PowerShell 5.1 has no $PSScriptRoot inside param defaults: resolve here.
if (-not $RepoRoot) {
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
    $RepoRoot = (Resolve-Path (Join-Path $here "..\..")).Path
}
$ErrorActionPreference = "Stop"

$python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$supervise = Join-Path $RepoRoot "tools\ops\supervise.py"
foreach ($p in @($python, $supervise)) {
    if (-not (Test-Path $p)) { throw "not found: $p" }
}

function New-PprTask([string]$Name, [string]$Service, $Trigger, [TimeSpan]$Limit) {
    $action = New-ScheduledTaskAction -Execute $python -Argument "`"$supervise`" $Service" `
        -WorkingDirectory $RepoRoot
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit $Limit `
        -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
    [pscustomobject]@{ Name = $Name; Action = $action; Trigger = $Trigger; Settings = $settings }
}

$startup = New-ScheduledTaskTrigger -AtStartup
$startup.Delay = "PT1M"
# Watchdog: every 5 minutes, for ever; ignored while the supervisor runs (IgnoreNew).
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 5)   # no duration = indefinitely
$nightly = New-ScheduledTaskTrigger -Daily -At $NightlyTime
$backup = New-ScheduledTaskTrigger -Daily -At $BackupTime
$tasks = @(
    (New-PprTask "PPR API" "api" @($startup, $watchdog) ([TimeSpan]::Zero)),
    (New-PprTask "PPR UI" "ui" @($startup, $watchdog) ([TimeSpan]::Zero)),
    (New-PprTask "PPR Nightly Sync" "nightly" $nightly (New-TimeSpan -Hours 3)),
    (New-PprTask "PPR Backup" "backup" $backup (New-TimeSpan -Hours 3))
)

foreach ($t in $tasks) {
    $first = @($t.Trigger)[0]
    $trig = if ($first.Delay) { "at start-up (delay $($first.Delay)) + every 5 min watchdog" } else { "daily at $(([datetime]$first.StartBoundary).ToString('HH:mm')) (server local time)" }
    Write-Output ("{0,-18} {1} | {2} {3} | restart every 1 min on failure | time limit {4}" -f `
        $t.Name, $trig, $t.Action.Execute, $t.Action.Arguments, $t.Settings.ExecutionTimeLimit)
}
if ($PrintOnly) {
    Write-Output "PrintOnly: nothing was registered."
    exit 0
}
if (-not $RunAsUser) { throw "give -RunAsUser (the Windows account the tasks run as)" }
$cred = Get-Credential -UserName $RunAsUser -Message "Password of the account the PPR tasks run as"
foreach ($t in $tasks) {
    Register-ScheduledTask -TaskPath "\PPR\" -TaskName $t.Name -Action $t.Action `
        -Trigger $t.Trigger -Settings $t.Settings -User $cred.UserName `
        -Password $cred.GetNetworkCredential().Password -RunLevel Limited -Force | Out-Null
    Write-Output "registered \PPR\$($t.Name)"
}
Write-Output "Start now (or reboot):  Start-ScheduledTask -TaskPath \PPR\ -TaskName 'PPR API'; Start-ScheduledTask -TaskPath \PPR\ -TaskName 'PPR UI'"
Write-Output "Check:                  powershell -ExecutionPolicy Bypass -File tools\ops\status.ps1"
