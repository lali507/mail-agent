# Registers two Windows scheduled tasks for the current user (run in PowerShell from the project folder):
#   MailAgent-Ingest : every hour 08:00-17:00, fetch + triage + draft (silent)
#   MailAgent-Review : daily at 18:00, toast notification + approval page in the browser
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
param(
    [string]$ReviewTime = "18:00",
    [string]$IngestStart = "08:00",
    [int]$IngestHours = 9
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$PythonW = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $PythonW)) { $PythonW = (Get-Command pythonw.exe).Source }

# Runs only while you are signed in, so the toast and browser appear on your screen.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 6) -MultipleInstances IgnoreNew

# Hourly ingest
$ingestAction = New-ScheduledTaskAction -Execute $PythonW -Argument "-m mail_agent ingest" -WorkingDirectory $ProjectDir
$ingestTrigger = New-ScheduledTaskTrigger -Daily -At $IngestStart
$ingestTrigger.Repetition = (New-ScheduledTaskTrigger -Once -At $IngestStart `
    -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Hours $IngestHours)).Repetition
Register-ScheduledTask -TaskName "MailAgent-Ingest" -Action $ingestAction -Trigger $ingestTrigger `
    -Settings $settings -Description "Mail agent: fetch, triage and draft replies" -Force | Out-Null

# Daily review (also does a final ingest first)
$reviewAction = New-ScheduledTaskAction -Execute $PythonW -Argument "-m mail_agent review" -WorkingDirectory $ProjectDir
$reviewTrigger = New-ScheduledTaskTrigger -Daily -At $ReviewTime
Register-ScheduledTask -TaskName "MailAgent-Review" -Action $reviewAction -Trigger $reviewTrigger `
    -Settings $settings -Description "Mail agent: daily approval of reply drafts" -Force | Out-Null

Write-Host "Installed: MailAgent-Ingest (hourly from $IngestStart) and MailAgent-Review (daily at $ReviewTime)."
Write-Host "Test now:  Start-ScheduledTask -TaskName MailAgent-Review"
