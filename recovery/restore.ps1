[CmdletBinding()]
param(
    [string]$Distro = "",
    [string]$BackupPath = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Get-TargetDistro {
    param([string]$Requested)

    $rawInstalled = @(& wsl.exe --list --quiet 2>$null)
    $wslExitCode = $LASTEXITCODE
    if ($wslExitCode -ne 0) {
        throw "Unable to list WSL Linux distributions (exit code $wslExitCode)."
    }
    $installed = @(
        $rawInstalled |
            ForEach-Object { $_.Trim().Trim([char]0) } |
            Where-Object { $_ }
    )
    if ($installed.Count -eq 0) {
        throw "No WSL Linux distribution is installed. See recovery/FRESH_MACHINE.md."
    }
    if ($Requested) {
        if ($installed -notcontains $Requested) {
            throw "Requested WSL distribution '$Requested' is not installed. Installed: $($installed -join ', ')"
        }
        return $Requested
    }
    foreach ($candidate in @("Ubuntu-24.04", "Ubuntu-22.04", "Ubuntu-20.04", "Ubuntu")) {
        if ($installed -contains $candidate) {
            return $candidate
        }
    }
    return $installed[0]
}

$targetDistro = Get-TargetDistro -Requested $Distro
$repoWsl = (& wsl.exe -d $targetDistro -- wslpath -a $repoRoot).Trim()
if (-not $repoWsl) {
    throw "Unable to translate repository path for WSL distro $targetDistro"
}

$arguments = @("$repoWsl/recovery/bootstrap-and-restore.sh")
if ($BackupPath) {
    $resolvedBackup = (Resolve-Path $BackupPath).Path
    $backupWsl = (& wsl.exe -d $targetDistro -- wslpath -a $resolvedBackup).Trim()
    $arguments += $backupWsl
}

& wsl.exe -d $targetDistro -- bash @arguments
if ($LASTEXITCODE -ne 0) {
    throw "MAR20 recovery failed with exit code $LASTEXITCODE"
}
