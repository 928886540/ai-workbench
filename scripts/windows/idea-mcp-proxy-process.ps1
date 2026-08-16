function Get-LocalListeningProcessId {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][int]$Port)

    $connection = Get-NetTCPConnection `
        -LocalAddress "127.0.0.1" `
        -LocalPort $Port `
        -State Listen `
        -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($connection) {
        return [int]$connection.OwningProcess
    }
    return $null
}

function Test-IdeaMcpProxyProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][string]$NodePath,
        [Parameter(Mandatory = $true)][string]$ProxyScriptPath
    )

    $process = Get-CimInstance Win32_Process `
        -Filter "ProcessId=$ProcessId" `
        -ErrorAction SilentlyContinue
    if (-not $process -or -not $process.ExecutablePath -or -not $process.CommandLine) {
        return $false
    }

    $expectedNodePath = [System.IO.Path]::GetFullPath($NodePath)
    $expectedScriptPath = [System.IO.Path]::GetFullPath($ProxyScriptPath)
    $actualNodePath = [System.IO.Path]::GetFullPath($process.ExecutablePath)
    $nodeArgument = '(?:"' + [regex]::Escape($expectedNodePath) + '"|' + [regex]::Escape($expectedNodePath) + ')'
    $scriptArgument = '(?:"' + [regex]::Escape($expectedScriptPath) + '"|' + [regex]::Escape($expectedScriptPath) + ')'
    $commandPattern = '(?i)^\s*' + $nodeArgument + '\s+' + $scriptArgument + '(?:\s|$)'
    return $actualNodePath -ieq $expectedNodePath -and $process.CommandLine -match $commandPattern
}

function Stop-IdeaMcpProxyListener {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$NodePath,
        [Parameter(Mandatory = $true)][string]$ProxyScriptPath,
        [int]$ExpectedProcessId = 0,
        [int]$TimeoutSeconds = 10
    )

    $listenerProcessId = Get-LocalListeningProcessId -Port 64343
    $processId = if ($ExpectedProcessId -gt 0) {
        $ExpectedProcessId
    }
    else {
        $listenerProcessId
    }
    if (-not $processId) {
        return $true
    }
    if (
        $ExpectedProcessId -gt 0 -and
        -not (Get-Process -Id $processId -ErrorAction SilentlyContinue) -and
        -not $listenerProcessId
    ) {
        return $true
    }
    if (-not (Test-IdeaMcpProxyProcess `
        -ProcessId $processId `
        -NodePath $NodePath `
        -ProxyScriptPath $ProxyScriptPath)) {
        throw "Refusing to stop unexpected process PID $processId on port 64343."
    }

    Stop-Process -Id $processId -Force -ErrorAction Stop
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while (
        (Get-Date) -lt $deadline -and
        (
            (Get-Process -Id $processId -ErrorAction SilentlyContinue) -or
            (Get-LocalListeningProcessId -Port 64343)
        )
    ) {
        Start-Sleep -Milliseconds 250
    }
    $remainingListenerProcessId = Get-LocalListeningProcessId -Port 64343
    if ($remainingListenerProcessId -and $remainingListenerProcessId -ne $processId) {
        throw "Unexpected process PID $remainingListenerProcessId took port 64343 during cleanup."
    }
    return (
        -not (Get-Process -Id $processId -ErrorAction SilentlyContinue) -and
        -not $remainingListenerProcessId
    )
}
