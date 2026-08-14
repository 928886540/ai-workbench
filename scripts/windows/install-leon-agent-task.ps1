[CmdletBinding()]
param(
    [string]$TaskName = "Leon Agent",
    [int]$Port = 8233
)

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$uvPath = (Get-Command uv -ErrorAction Stop).Source
$powershellPath = (Get-Command powershell.exe -ErrorAction Stop).Source
$currentUser = "$env:USERDOMAIN\$env:USERNAME"
$command = "& '$uvPath' run leon-server --host 127.0.0.1 --port $Port"

$action = New-ScheduledTaskAction `
    -Execute $powershellPath `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$command`"" `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval ([TimeSpan]::FromMinutes(1)) `
    -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited
$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Leon Agent Gateway via uv; exposed only through Cloudflare Tunnel."

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Scheduled task '$TaskName' is running on http://127.0.0.1:$Port"
