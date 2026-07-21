param([switch]$WhatIfOnly)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$Targets = @("local")

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
