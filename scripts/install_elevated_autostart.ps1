$ErrorActionPreference = "Stop"

$taskName = "Bridge-U-F Autostart"
$taskPath = "\"
$startupBat = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\bridge-u-f-autostart.bat"
$bridgeBat = "C:\Users\thene\projects\bridge-u-f\bridge-u.bat"
$arguments = '/c ""C:\Users\thene\projects\bridge-u-f\bridge-u.bat" --resume-last --workbench --no-pause"'

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 0)

Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

if (Test-Path $startupBat) {
    Remove-Item $startupBat -Force
}

Write-Host "Installed scheduled task '$taskName' with highest privileges."
Write-Host "Removed Startup-folder fallback: $startupBat"
