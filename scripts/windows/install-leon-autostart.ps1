[CmdletBinding()]
param(
    [string]$GatewayTaskName = "Leon Agent",
    [string]$IdeaTaskName = "IDEA MCP Auth Proxy",
    [int]$StartupDelaySeconds = 30,
    [switch]$SkipStart
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$wrapperPath = (Resolve-Path (Join-Path $PSScriptRoot "run-leon-autostart-service.ps1")).Path
$powershellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$currentUser = "$env:USERDOMAIN\$env:USERNAME"
$userProfile = $env:USERPROFILE

function New-WrapperAction {
    param([ValidateSet("Gateway", "IdeaMcpProxy")][string]$Service)
    $argument = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden " +
        "-ExecutionPolicy Bypass -File `"$wrapperPath`" " +
        "-Service $Service -ProjectRoot `"$projectRoot`" " +
        "-UserProfile `"$userProfile`" -DelaySeconds $StartupDelaySeconds"
    return New-ScheduledTaskAction `
        -Execute $powershellPath `
        -Argument $argument `
        -WorkingDirectory $projectRoot
}

function Stop-TaskIfPresent {
    param([string]$TaskName)
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        return
    }
    if ($existing.State -eq "Running") {
        Write-Host "Stopping existing task '$TaskName' before replacement..."
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            Start-Sleep -Milliseconds 500
            $state = (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State
            if ($state -ne "Running") {
                break
            }
        }
    }
}

$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $currentUser),
    (New-ScheduledTaskTrigger -AtStartup)
)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 5 `
    -RestartInterval ([TimeSpan]::FromMinutes(1)) `
    -StartWhenAvailable `
    -Hidden
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

$definitions = @(
    @{
        Name = $GatewayTaskName
        Service = "Gateway"
        Description = "Leon Web Gateway on 127.0.0.1:8233 with a user-level .leon config."
    },
    @{
        Name = $IdeaTaskName
        Service = "IdeaMcpProxy"
        Description = "Bearer-authenticated IDEA MCP proxy on 127.0.0.1:64343."
    }
)

foreach ($definition in $definitions) {
    Stop-TaskIfPresent -TaskName $definition.Name
    $action = New-WrapperAction -Service $definition.Service
    $task = New-ScheduledTask `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -Principal $principal `
        -Description $definition.Description
    Register-ScheduledTask -TaskName $definition.Name -InputObject $task -Force | Out-Null
    Write-Host "Registered '$($definition.Name)' for $currentUser ($($definition.Service))."
    if (-not $SkipStart) {
        Start-ScheduledTask -TaskName $definition.Name
        Write-Host "Started '$($definition.Name)'."
    }
}

Write-Host "Gateway task: $GatewayTaskName"
Write-Host "IDEA proxy task: $IdeaTaskName"
Write-Host "Logs: $userProfile\.leon\logs"
