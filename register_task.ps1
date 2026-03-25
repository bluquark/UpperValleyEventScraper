# Registers (or re-registers) a nightly Windows Scheduled Task to run the scraper.
# Run once from an elevated PowerShell prompt:
#   Set-ExecutionPolicy -Scope Process Bypass
#   .\register_task.ps1

$TaskName  = "UpperValleyEventScraper"
$ScriptDir = "C:\myprojects\UpperValleyEventScraper"
$LogFile   = "$ScriptDir\output\scraper.log"

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NonInteractive -ExecutionPolicy Bypass -File `"$ScriptDir\run_scraper.ps1`"" `
    -WorkingDirectory $ScriptDir

$Trigger = New-ScheduledTaskTrigger -Daily -At "02:00"

# -StartWhenAvailable catches up if the machine was off at 2am
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

# Run as the current user, only when logged in (avoids credential prompts)
$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Highest

# Remove existing task if present, then register fresh
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName  $TaskName `
    -Action    $Action `
    -Trigger   $Trigger `
    -Settings  $Settings `
    -Principal $Principal `
    -Description "Nightly scrape of Upper Valley events"

Write-Host ""
Write-Host "Task '$TaskName' registered. Runs daily at 02:00."
Write-Host "To run it now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To check logs:  Get-Content '$LogFile' -Tail 50"
Write-Host "To remove it:   Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
