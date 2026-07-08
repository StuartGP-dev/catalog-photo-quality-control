param(
    [switch]$IncludeArchives,
    [switch]$WhatIfOnly
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$Targets = @(
    "local",
    "target_filter_archive"
)

if ($IncludeArchives) {
    $Targets += Get-ChildItem -Path $RepoRoot -Filter "*.zip" -File | ForEach-Object { $_.FullName }
}

foreach ($Target in $Targets) {
    if (Test-Path $Target) {
        if ($WhatIfOnly) {
            Write-Host "Would remove: $Target"
        } else {
            Remove-Item -Recurse -Force $Target
            Write-Host "Removed: $Target"
        }
    }
}

Write-Host "Cleanup done."
