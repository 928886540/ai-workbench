[CmdletBinding()]
param(
    [string]$IdeaPath = "D:\JetBrains\IntelliJ IDEA 2026.1\bin\idea64.exe",
    [string]$NodePath = "D:\sfotwore\nodejs\node.exe",
    [string]$ProxyScriptPath = "D:\cloudflared\idea-mcp-auth-proxy.mjs",
    [int]$StartupTimeoutSeconds = 30,
    [int]$ProxyTimeoutSeconds = 5,
    [int]$ExitGraceSeconds = 5
)

$ErrorActionPreference = "Stop"

$ideaPathFull = [System.IO.Path]::GetFullPath($IdeaPath)
$nodePathFull = [System.IO.Path]::GetFullPath($NodePath)
$proxyScriptPathFull = [System.IO.Path]::GetFullPath($ProxyScriptPath)
$ideaBinDirectory = Split-Path -Parent $ideaPathFull
$ideaWorkingDirectory = Split-Path -Parent $ideaBinDirectory
$ideaProcessName = [System.IO.Path]::GetFileNameWithoutExtension($ideaPathFull)
$proxyWorkingDirectory = Split-Path -Parent $proxyScriptPathFull
$proxyTokenPath = Join-Path $proxyWorkingDirectory "idea-mcp-auth-token.txt"
$helperPath = Join-Path $PSScriptRoot "idea-mcp-proxy-process.ps1"
$logRoot = Join-Path $env:USERPROFILE ".leon\logs"
$logPath = Join-Path $logRoot "IdeaLauncher.log"
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null

. $helperPath

function Write-LauncherLog {
    param([string]$Message)
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "o"), $PID, $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Get-TargetIdeaProcesses {
    @(Get-Process -Name $ideaProcessName -ErrorAction SilentlyContinue | Where-Object {
        try {
            $_.Path -and ([System.IO.Path]::GetFullPath($_.Path) -ieq $ideaPathFull)
        }
        catch {
            $false
        }
    })
}

function Wait-ForValidatedProxyListener {
    param([int]$TimeoutSeconds)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $listenerProcessId = Get-LocalListeningProcessId -Port 64343
        if ($listenerProcessId) {
            if (-not (Test-IdeaMcpProxyProcess `
                -ProcessId $listenerProcessId `
                -NodePath $nodePathFull `
                -ProxyScriptPath $proxyScriptPathFull)) {
                throw "Port 64343 is owned by an unexpected process (PID $listenerProcessId)."
            }
            return $listenerProcessId
        }
        Start-Sleep -Milliseconds 250
    }
    throw "IDEA MCP proxy did not become ready on 127.0.0.1:64343 within $TimeoutSeconds seconds."
}

foreach ($requiredFile in @(
    $ideaPathFull,
    $nodePathFull,
    $proxyScriptPathFull,
    $proxyTokenPath,
    $helperPath
)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        Write-LauncherLog "ERROR required file missing: $requiredFile"
        throw "Required file not found: $requiredFile"
    }
}

$mutex = [System.Threading.Mutex]::new($false, "Global\LeonIdeaMcpLauncher")
$ownsLifecycle = $false
$proxyProcessId = Get-LocalListeningProcessId -Port 64343
try {
    try {
        $ownsLifecycle = $mutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $ownsLifecycle = $true
    }

    if ($ownsLifecycle) {
        if ($proxyProcessId) {
            if (-not (Test-IdeaMcpProxyProcess `
                -ProcessId $proxyProcessId `
                -NodePath $nodePathFull `
                -ProxyScriptPath $proxyScriptPathFull)) {
                throw "Port 64343 is owned by an unexpected process (PID $proxyProcessId)."
            }
            Write-LauncherLog "adopted existing proxy pid=$proxyProcessId"
        }
        else {
            $stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
            $stdoutPath = Join-Path $logRoot "IdeaMcpProxy-$stamp.stdout.log"
            $stderrPath = Join-Path $logRoot "IdeaMcpProxy-$stamp.stderr.log"
            $proxyProcess = Start-Process `
                -FilePath $nodePathFull `
                -ArgumentList @($proxyScriptPathFull) `
                -WorkingDirectory $proxyWorkingDirectory `
                -WindowStyle Hidden `
                -RedirectStandardOutput $stdoutPath `
                -RedirectStandardError $stderrPath `
                -PassThru
            $proxyProcessId = $proxyProcess.Id
            Write-LauncherLog "started proxy pid=$proxyProcessId"
        }
        $proxyProcessId = Wait-ForValidatedProxyListener -TimeoutSeconds $ProxyTimeoutSeconds
        Write-LauncherLog "proxy port 64343 is listening pid=$proxyProcessId"
    }
    else {
        # A second shortcut click must wait for the owner launcher to publish a
        # validated proxy before it can start another IDEA instance.
        $proxyProcessId = Wait-ForValidatedProxyListener -TimeoutSeconds $ProxyTimeoutSeconds
        Write-LauncherLog "adopted validated proxy pid=$proxyProcessId from owner launcher"
    }

    Start-Process -FilePath $ideaPathFull -WorkingDirectory $ideaWorkingDirectory | Out-Null
    Write-LauncherLog "started IDEA executable=$ideaPathFull"

    if (-not $ownsLifecycle) {
        return
    }

    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    while ((Get-Date) -lt $deadline -and (Get-TargetIdeaProcesses).Count -eq 0) {
        Start-Sleep -Milliseconds 500
    }
    if ((Get-TargetIdeaProcesses).Count -eq 0) {
        Write-LauncherLog "WARNING IDEA process was not observed before timeout"
        return
    }

    Write-LauncherLog "IDEA process observed; monitoring lifecycle"
    while ($true) {
        while ((Get-TargetIdeaProcesses).Count -gt 0) {
            Start-Sleep -Seconds 1
        }
        Start-Sleep -Seconds $ExitGraceSeconds
        if ((Get-TargetIdeaProcesses).Count -eq 0) {
            break
        }
    }
    Write-LauncherLog "all IDEA processes exited"
}
catch {
    Write-LauncherLog ("ERROR {0}: {1}" -f $_.Exception.GetType().Name, $_.Exception.Message)
    throw
}
finally {
    try {
        if ($ownsLifecycle -and $proxyProcessId) {
            if (Stop-IdeaMcpProxyListener `
                -NodePath $nodePathFull `
                -ProxyScriptPath $proxyScriptPathFull `
                -ExpectedProcessId $proxyProcessId) {
                Write-LauncherLog "stopped proxy pid=$proxyProcessId; port 64343 released"
            }
            else {
                Write-LauncherLog "WARNING proxy pid=$proxyProcessId did not stop cleanly"
            }
        }
    }
    catch {
        try {
            Write-LauncherLog ("ERROR proxy cleanup failed: {0}" -f $_.Exception.Message)
        }
        catch { }
    }
    finally {
        if ($ownsLifecycle) {
            try { $mutex.ReleaseMutex() } catch { }
        }
        $mutex.Dispose()
    }
}
