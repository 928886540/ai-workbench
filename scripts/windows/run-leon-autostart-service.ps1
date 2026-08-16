[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Gateway", "IdeaMcpProxy")]
    [string]$Service,
    [string]$ProjectRoot = "D:\apiWorkSpace\ai-workbench",
    [string]$UserProfile = $env:USERPROFILE,
    [int]$DelaySeconds = 30
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
        $port = 8233
        $mutexName = "Global\LeonAgentGateway8233"
        $configPath = Join-Path $UserProfile ".leon\config.toml"
        $workingDirectory = (Resolve-Path -LiteralPath $ProjectRoot).Path
        $executable = Join-Path $workingDirectory ".venv\Scripts\leon-server.exe"
        $arguments = @("--host", "127.0.0.1", "--port", "$port")
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

    if (Test-LocalPort -Port $port) {
        Write-RunLog "port $port is already listening; leaving existing process untouched"
        exit 0
    }

    if ($Service -eq "Gateway") {
        $env:LEON_CONFIG_FILE = [System.IO.Path]::GetFullPath($configPath)
        Write-RunLog "using user config file (path omitted from child arguments)"
    }

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $stdoutPath = Join-Path $logRoot "$Service-$stamp.stdout.log"
    $stderrPath = Join-Path $logRoot "$Service-$stamp.stderr.log"
    Write-RunLog "starting executable=$executable working_directory=$workingDirectory"
    $child = Start-Process `
        -FilePath $executable `
        -ArgumentList $arguments `
        -WorkingDirectory $workingDirectory `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru
    Write-RunLog "child started pid=$($child.Id) stdout=$stdoutPath stderr=$stderrPath"
    $child.WaitForExit()
    $exitCode = [int]$child.ExitCode
    Write-RunLog "child exited code=$exitCode"
    exit $exitCode
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
