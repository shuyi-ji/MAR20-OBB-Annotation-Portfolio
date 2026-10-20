[CmdletBinding()]
param(
    [string]$Distro = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repository = "shuyi-ji/MAR20-OBB-Annotation-Portfolio"
$checkpointTag = "task25-checkpoint-latest"
$snapshotDir = Join-Path $repoRoot ".runtime\mar20-cvat\snapshots\latest"

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
$ghCommand = Get-Command gh -ErrorAction SilentlyContinue
if (-not $ghCommand) {
    throw "GitHub CLI is not installed. Run: winget install -e --id GitHub.cli"
}
& gh auth status *> $null
if ($LASTEXITCODE -ne 0) {
    throw "GitHub CLI is not authorized. Run: gh auth login"
}

$repoWsl = (& wsl.exe -d $targetDistro -- wslpath -a $repoRoot).Trim()
if (-not $repoWsl) {
    throw "Unable to translate repository path for WSL distro $targetDistro"
}

& wsl.exe -d $targetDistro -- env MAR20_SKIP_PUBLISH=1 bash "$repoWsl/recovery/checkpoint-and-publish.sh"
if ($LASTEXITCODE -ne 0) {
    throw "MAR20 checkpoint export failed with exit code $LASTEXITCODE"
}

$backup = Join-Path $snapshotDir "task_25_MAR20_Portfolio_OBB_Review_200-latest.zip"
$labels = Join-Path $snapshotDir "task_25_MAR20_Portfolio_OBB_Review_200-labels-latest.zip"
$manifest = Join-Path $snapshotDir "snapshot_manifest.json"
foreach ($path in @($backup, $labels, $manifest)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Checkpoint asset is missing: $path"
    }
}

& gh release view $checkpointTag --repo $repository *> $null
if ($LASTEXITCODE -ne 0) {
    & gh release create $checkpointTag `
        --repo $repository `
        --title "Task #25 latest working checkpoint" `
        --notes-file (Join-Path $PSScriptRoot "CHECKPOINT_NOTES.md")
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to create GitHub checkpoint Release."
    }
}

& gh release upload $checkpointTag $backup $labels --repo $repository --clobber
if ($LASTEXITCODE -ne 0) {
    throw "Unable to upload checkpoint backup or label archive."
}

# The manifest is uploaded last because restore treats it as the integrity pointer.
& gh release upload $checkpointTag $manifest --repo $repository --clobber
if ($LASTEXITCODE -ne 0) {
    throw "Unable to upload checkpoint manifest."
}

Write-Host "[PASS] Latest CVAT task and labels published to GitHub Release $checkpointTag"
