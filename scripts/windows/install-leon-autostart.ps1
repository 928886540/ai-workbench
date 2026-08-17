[CmdletBinding()]
param(
    [string]$GatewayTaskName = "Leon Agent",
    [string]$IdeaTaskName = "IDEA MCP Auth Proxy",
    [int]$StartupDelaySeconds = 30,
    [ValidateRange(1, 999)]
    [int]$RestartCount = 20,
    [switch]$SkipStart,
    [switch]$SkipIdeaShortcutUpdate,
    [switch]$SkipIdeaProxyLifecycle
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$wrapperPath = (Resolve-Path (Join-Path $PSScriptRoot "run-leon-autostart-service.ps1")).Path
$proxyProcessHelpers = (Resolve-Path (Join-Path $PSScriptRoot "idea-mcp-proxy-process.ps1")).Path
$powershellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$currentUser = "$env:USERDOMAIN\$env:USERNAME"
$userProfile = $env:USERPROFILE

. $proxyProcessHelpers

function New-WrapperAction {
    param(
        [ValidateSet("Gateway", "IdeaMcpProxy")][string]$Service,
        [int]$DelaySeconds
    )
    $argument = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden " +
        "-ExecutionPolicy Bypass -File `"$wrapperPath`" " +
        "-Service $Service -ProjectRoot `"$projectRoot`" " +
        "-UserProfile `"$userProfile`" -DelaySeconds $DelaySeconds"
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

$gatewayTriggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $currentUser),
    (New-ScheduledTaskTrigger -AtStartup)
)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount $RestartCount `
    -RestartInterval ([TimeSpan]::FromMinutes(1)) `
    -StartWhenAvailable `
    -Hidden
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

$definitions = @(@{
    Name = $GatewayTaskName
    Service = "Gateway"
    DelaySeconds = $StartupDelaySeconds
    Triggers = $gatewayTriggers
    StartNow = -not $SkipStart
    Description = "Leon Web Gateway on 127.0.0.1:8233 with a user-level .leon config."
})

foreach ($definition in $definitions) {
    Stop-TaskIfPresent -TaskName $definition.Name
    $action = New-WrapperAction `
        -Service $definition.Service `
        -DelaySeconds $definition.DelaySeconds
    $taskArguments = @{
        Action = $action
        Settings = $settings
        Principal = $principal
        Description = $definition.Description
    }
    if ($definition.Triggers.Count -gt 0) {
        $taskArguments.Trigger = $definition.Triggers
    }
    $task = New-ScheduledTask @taskArguments
    Register-ScheduledTask -TaskName $definition.Name -InputObject $task -Force | Out-Null
    Write-Host "Registered '$($definition.Name)' for $currentUser ($($definition.Service))."
    if ($definition.StartNow) {
        Start-ScheduledTask -TaskName $definition.Name
        Write-Host "Started '$($definition.Name)'."
    }
}

$ideaTask = Get-ScheduledTask -TaskName $IdeaTaskName -ErrorAction SilentlyContinue
if ($ideaTask) {
    Stop-TaskIfPresent -TaskName $IdeaTaskName
    Unregister-ScheduledTask -TaskName $IdeaTaskName -Confirm:$false
    Write-Host "Removed legacy IDEA proxy task '$IdeaTaskName'."
}

$nodePath = "D:\sfotwore\nodejs\node.exe"
$proxyScriptPath = "D:\cloudflared\idea-mcp-auth-proxy.mjs"
if (-not $SkipIdeaProxyLifecycle) {
    if (-not (Stop-IdeaMcpProxyListener -NodePath $nodePath -ProxyScriptPath $proxyScriptPath)) {
        throw "IDEA MCP proxy port 64343 did not stop cleanly."
    }

    if (-not $SkipIdeaShortcutUpdate) {
        $shortcutInstaller = Join-Path $PSScriptRoot "configure-idea-mcp-shortcuts.ps1"
        & $shortcutInstaller
    }
}
else {
    Write-Host "IDEA proxy lifecycle unchanged (explicitly skipped)."
}

Write-Host "Gateway task: $GatewayTaskName"
Write-Host "IDEA proxy: on demand from the IDEA launcher (no scheduled task)"
Write-Host "Logs: $userProfile\.leon\logs"
