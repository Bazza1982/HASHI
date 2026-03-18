$ErrorActionPreference = "Stop"

$taskName = "HASHI2 Autostart (WSL)"
$taskPath = "\"
$startupBat = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\hashi2-autostart.bat"

# Execute wsl.exe to run the HASHI2 Linux instance directly
$wslDistro = "Ubuntu-22.04"
$wslProjectDir = "/home/lily/projects/hashi2"
$bashScript = "./bin/bridge-u.sh"

# Note: Using --api-gateway instead of --workbench as requested. --force avoids the prompt on reboot.
$arguments = "-d $wslDistro --cd $wslProjectDir -e bash $bashScript --resume-last --api-gateway --force"

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
$action = New-ScheduledTaskAction -Execute "wsl.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 0)

Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

if (Test-Path $startupBat) {
    Remove-Item $startupBat -Force
}

Write-Host "Installed scheduled task '$taskName' with highest privileges."
Write-Host "Removed Startup-folder fallback: $startupBat"
