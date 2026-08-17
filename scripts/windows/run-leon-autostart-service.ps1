[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Gateway", "IdeaMcpProxy")]
    [string]$Service,
    [string]$ProjectRoot = "D:\apiWorkSpace\ai-workbench",
    [string]$UserProfile = $env:USERPROFILE,
    [int]$DelaySeconds = 30,
    [ValidateRange(1, 65535)]
    [int]$GatewayPort = 8233,
    [ValidateRange(1, 999)]
    [int]$RestartCount = 20,
    [ValidateRange(1, 3600)]
    [int]$RestartDelaySeconds = 60,
    [ValidateRange(1, 86400)]
    [int]$RestartResetSeconds = 300
)

$ErrorActionPreference = "Stop"

$logRoot = Join-Path $UserProfile ".leon\logs"
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$logPath = Join-Path $logRoot "$Service-wrapper.log"

function Write-RunLog {
    param([string]$Message)
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "o"), $PID, $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Test-LocalPort {
    param([int]$Port)
    $client = [System.Net.Sockets.TcpClient]::new()
    $waitHandle = $null
    try {
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        $waitHandle = $async.AsyncWaitHandle
        if (-not $waitHandle.WaitOne(750)) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($waitHandle) {
            $waitHandle.Dispose()
        }
        $client.Dispose()
    }
}

switch ($Service) {
    "Gateway" {
        $port = $GatewayPort
        $mutexName = "Global\LeonAgentGateway$port"
        $configPath = Join-Path $UserProfile ".leon\config.toml"
        $workingDirectory = (Resolve-Path -LiteralPath $ProjectRoot).Path
        # pythonw.exe is a GUI-subsystem launcher: it prevents a console window
        # (and its taskbar button) from being created for the background Gateway.
        $executable = Join-Path $workingDirectory ".venv\Scripts\pythonw.exe"
        $arguments = @(
            "-m", "leon_agent.gateway.server",
            "--host", "127.0.0.1",
            "--port", "$port"
        )
        $requiredFiles = @($configPath, $executable)
    }
    "IdeaMcpProxy" {
        $port = 64343
        $mutexName = "Global\IdeaMcpAuthProxy64343"
        $workingDirectory = "D:\cloudflared"
        $executable = "D:\sfotwore\nodejs\node.exe"
        $scriptPath = Join-Path $workingDirectory "idea-mcp-auth-proxy.mjs"
        $tokenPath = Join-Path $workingDirectory "idea-mcp-auth-token.txt"
        $arguments = @($scriptPath)
        $requiredFiles = @($executable, $scriptPath, $tokenPath)
    }
}

$mutex = $null
$mutexHeld = $false
try {
    Write-RunLog "wrapper entered service=$Service port=$port"
    if ($DelaySeconds -gt 0) {
        Write-RunLog "startup delay ${DelaySeconds}s"
        Start-Sleep -Seconds $DelaySeconds
    }

    foreach ($requiredFile in $requiredFiles) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            Write-RunLog "ERROR required file missing: $requiredFile"
            exit 2
        }
    }

    $mutex = [System.Threading.Mutex]::new($false, $mutexName)
    try {
        $mutexHeld = $mutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $mutexHeld = $true
    }
    if (-not $mutexHeld) {
        Write-RunLog "another wrapper instance owns mutex; exiting"
        exit 0
    }

    if ($Service -eq "Gateway") {
        $env:LEON_CONFIG_FILE = [System.IO.Path]::GetFullPath($configPath)
        Write-RunLog "using user config file (path omitted from child arguments)"
    }

    $remainingRestarts = $RestartCount
    while ($true) {
        if (Test-LocalPort -Port $port) {
            if ($Service -ne "Gateway") {
                Write-RunLog "port $port is already listening; leaving existing process untouched"
                exit 0
            }
            # Do not kill or replace a process that owns the port. Keep the
            # hidden task alive and take ownership only after it is released.
            Write-RunLog "port $port is already listening; monitoring until it is released"
            while (Test-LocalPort -Port $port) {
                Start-Sleep -Seconds 5
            }
            Write-RunLog "port $port released; starting managed Gateway"
        }

        $stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
        $stdoutPath = Join-Path $logRoot "$Service-$stamp.stdout.log"
        $stderrPath = Join-Path $logRoot "$Service-$stamp.stderr.log"
        Write-RunLog "starting executable=$executable working_directory=$workingDirectory"
        $startedAt = Get-Date
        $child = Start-Process `
            -FilePath $executable `
            -ArgumentList $arguments `
            -WorkingDirectory $workingDirectory `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -PassThru
        Write-RunLog "child started pid=$($child.Id) stdout=$stdoutPath stderr=$stderrPath"
        $child.WaitForExit()
        $exitCode = [int]$child.ExitCode
        $runSeconds = ((Get-Date) - $startedAt).TotalSeconds
        Write-RunLog "child exited code=$exitCode runtime_seconds=$([int]$runSeconds)"

        if ($Service -ne "Gateway") {
            exit $exitCode
        }
        if ($runSeconds -ge $RestartResetSeconds) {
            $remainingRestarts = $RestartCount
        }
        $remainingRestarts--
        if ($remainingRestarts -le 0) {
            $taskExitCode = if ($exitCode -eq 0) { 1 } else { $exitCode }
            Write-RunLog "Gateway restart budget exhausted; returning task code=$taskExitCode"
            exit $taskExitCode
        }

        Write-RunLog "Gateway stopped; retrying in ${RestartDelaySeconds}s remaining_attempts=$remainingRestarts"
        Start-Sleep -Seconds $RestartDelaySeconds
    }
}
catch {
    Write-RunLog ("ERROR {0}: {1}" -f $_.Exception.GetType().Name, $_.Exception.Message)
    exit 1
}
finally {
    if ($mutexHeld -and $mutex) {
        try { $mutex.ReleaseMutex() } catch { }
    }
    if ($mutex) {
        $mutex.Dispose()
    }
}
