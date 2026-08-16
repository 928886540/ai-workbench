[CmdletBinding()]
param(
    [string]$IdeaPath = "D:\JetBrains\IntelliJ IDEA 2026.1\bin\idea64.exe",
    [string]$LauncherPath = (Join-Path $PSScriptRoot "start-idea-with-mcp-proxy.ps1"),
    [switch]$Restore
)

$ErrorActionPreference = "Stop"

$powershellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$ideaPathFull = [System.IO.Path]::GetFullPath($IdeaPath)
$ideaDirectory = Split-Path -Parent (Split-Path -Parent $ideaPathFull)
$shortcutPaths = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "IntelliJ IDEA 2026.1.lnk"),
    (Join-Path ([Environment]::GetFolderPath("CommonStartMenu")) "Programs\JetBrains\IntelliJ IDEA 2026.1.lnk"),
    (Join-Path $env:APPDATA "Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar\IntelliJ IDEA 2026.1.lnk")
)

if (-not $Restore -and -not (Test-Path -LiteralPath $LauncherPath -PathType Leaf)) {
    throw "IDEA MCP launcher not found: $LauncherPath"
}

$shell = New-Object -ComObject WScript.Shell
$updated = 0
$changedPaths = @()
try {
    foreach ($shortcutPath in $shortcutPaths) {
        $backupPath = "$shortcutPath.leon-original"
        if ($Restore) {
            if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
                Copy-Item -LiteralPath $backupPath -Destination $shortcutPath -Force
                Write-Host "Restored: $shortcutPath"
                $updated++
            }
            else {
                Write-Warning "Backup not found: $backupPath"
            }
            continue
        }

        if (-not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) {
            Write-Warning "Shortcut not found: $shortcutPath"
            continue
        }

        if (-not (Test-Path -LiteralPath $backupPath -PathType Leaf)) {
            Copy-Item -LiteralPath $shortcutPath -Destination $backupPath
            (Get-Item -LiteralPath $backupPath).Attributes = `
                (Get-Item -LiteralPath $backupPath).Attributes -bor [System.IO.FileAttributes]::Hidden
        }

        $changedPaths += $shortcutPath
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $powershellPath
        $shortcut.Arguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden " +
            "-ExecutionPolicy Bypass -File `"$LauncherPath`""
        $shortcut.WorkingDirectory = $ideaDirectory
        $shortcut.IconLocation = "$ideaPathFull,0"
        $shortcut.Description = "IntelliJ IDEA 2026.1 with the authenticated MCP proxy"
        $shortcut.Save()

        $savedShortcut = $shell.CreateShortcut($shortcutPath)
        if (
            $savedShortcut.TargetPath -ine $powershellPath -or
            -not $savedShortcut.Arguments.Contains($LauncherPath)
        ) {
            throw "Shortcut verification failed: $shortcutPath"
        }
        Write-Host "Updated: $shortcutPath"
        $updated++
    }
}
catch {
    foreach ($changedPath in $changedPaths) {
        $backupPath = "$changedPath.leon-original"
        if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
            Copy-Item -LiteralPath $backupPath -Destination $changedPath -Force -ErrorAction SilentlyContinue
        }
    }
    throw
}

if ($updated -eq 0) {
    throw "No IDEA shortcuts were updated."
}
