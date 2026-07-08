param(
    [string]$Listing = "bijoux/O/O18",
    [string]$RunLabel = "",
    [switch]$IncludeStageReports,
    [switch]$IncludeRendered,
    [switch]$IncludeLocalDb
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

function Safe-Name([string]$Value) {
    $safe = $Value.Trim().Replace("\", "/") -replace "[^A-Za-z0-9_.-]+", "_"
    $safe = $safe.Trim("_")
    if ([string]::IsNullOrWhiteSpace($safe)) { return "listing" }
    return $safe
}

function Redact-Dsn([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return "" }
    return ($Value -replace "://([^:]+):([^@]+)@", "://`$1:***@")
}

function Copy-DebugFile([string]$SourcePath, [string]$RelativeDest) {
    if ([string]::IsNullOrWhiteSpace($SourcePath)) { return }
    $src = $SourcePath
    if ($src.StartsWith("file:///")) {
        $src = $src.Substring(8).Replace("/", "\")
    }
    if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { return }

    $dest = Join-Path $TempDir $RelativeDest
    New-Item -ItemType Directory -Force -Path (Split-Path $dest -Parent) | Out-Null
    Copy-Item -LiteralPath $src -Destination $dest -Force
}

function Copy-DebugTree([string]$SourceDir, [string]$RelativeDest, [string[]]$ExcludeDirNames = @(), [string[]]$ExcludeFileNames = @()) {
    if ([string]::IsNullOrWhiteSpace($SourceDir)) { return }
    if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) { return }

    $root = (Resolve-Path -LiteralPath $SourceDir).Path
    Get-ChildItem -LiteralPath $root -Recurse -File | ForEach-Object {
        $file = $_
        $relative = $file.FullName.Substring($root.Length).TrimStart("\", "/")
        $parts = $relative -split "[\\/]"
        foreach ($excluded in $ExcludeDirNames) {
            if ($parts -contains $excluded) { return }
        }
        if ($ExcludeFileNames -contains $file.Name) { return }

        $destRel = Join-Path $RelativeDest $relative
        Copy-DebugFile $file.FullName $destRel
    }
}

function Extract-PathsFromLog([string]$LogPath) {
    $paths = [ordered]@{}
    if (-not (Test-Path -LiteralPath $LogPath -PathType Leaf)) { return $paths }

    Get-Content -LiteralPath $LogPath | ForEach-Object {
        $line = $_
        foreach ($prefix in @("Rapport JSON:", "Rapport CSV:", "Rapport HTML:", "Planche avant/apres:", "DB recettes:")) {
            if ($line.StartsWith($prefix)) {
                $key = ($prefix -replace "[^A-Za-z]", "_").Trim("_")
                $paths[$key] = $line.Substring($prefix.Length).Trim()
            }
        }
    }
    return $paths
}

$SafeListing = Safe-Name $Listing
$SeqRoot = Join-Path $RepoRoot "local\debug_catalog_photo_control\bench_sequences\$SafeListing"
if (-not (Test-Path -LiteralPath $SeqRoot -PathType Container)) {
    throw "Bench sequence root not found: $SeqRoot"
}

if ([string]::IsNullOrWhiteSpace($RunLabel)) {
    $SeqDir = Get-ChildItem -LiteralPath $SeqRoot -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $SeqDir) { throw "No bench sequence found under: $SeqRoot" }
} else {
    $candidate = Join-Path $SeqRoot $RunLabel
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
        throw "Bench sequence not found: $candidate"
    }
    $SeqDir = Get-Item -LiteralPath $candidate
}

$Timestamp = Get-Date -Format yyyyMMdd_HHmmss
$BundleRoot = Join-Path $RepoRoot "local\debug_bundles"
New-Item -ItemType Directory -Force -Path $BundleRoot | Out-Null
$ZipPath = Join-Path $BundleRoot ("bench_debug_{0}_{1}_{2}.zip" -f $SafeListing, $SeqDir.Name, $Timestamp)
$TempDir = Join-Path $RepoRoot ("local\_debug_bundle_tmp_{0}_{1}" -f $SafeListing, $Timestamp)
if (Test-Path -LiteralPath $TempDir) { Remove-Item -LiteralPath $TempDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $TempDir | Out-Null

try {
    $manifest = [ordered]@{
        created_at = (Get-Date).ToString("o")
        repo_root = $RepoRoot.Path
        listing = $Listing
        safe_listing = $SafeListing
        sequence_dir = $SeqDir.FullName
        include_stage_reports = [bool]$IncludeStageReports
        include_rendered = [bool]$IncludeRendered
        include_local_db = [bool]$IncludeLocalDb
        catalog_db_dsn_redacted = (Redact-Dsn $env:CATALOG_DB_DSN)
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -Path (Join-Path $TempDir "manifest.json") -Encoding UTF8

    try { git rev-parse HEAD | Set-Content -Path (Join-Path $TempDir "git_head.txt") -Encoding UTF8 } catch {}
    try { git status --short | Set-Content -Path (Join-Path $TempDir "git_status_short.txt") -Encoding UTF8 } catch {}
    try {
        python -m common.catalog_photo_control.catalog_db_summary --annonce-key $Listing *> (Join-Path $TempDir "catalog_db_summary.txt")
    } catch {
        "catalog_db_summary failed: $($_.Exception.Message)" | Set-Content -Path (Join-Path $TempDir "catalog_db_summary.txt") -Encoding UTF8
    }

    # Always include the compact sequence directory: logs, diverse JSON/CSV/HTML, import log.
    Copy-DebugTree $SeqDir.FullName "bench_sequence" -ExcludeDirNames @("rendered")

    # Include compact artifacts from stage report directories, excluding rendered images by default.
    $stageLogs = @(
        Join-Path $SeqDir.FullName "stage1_symmetric_target_hunt.log",
        Join-Path $SeqDir.FullName "stage2_cluster_aware_hunt.log"
    )

    $stageNumber = 0
    foreach ($log in $stageLogs) {
        $stageNumber += 1
        $paths = Extract-PathsFromLog $log
        $jsonPath = $paths["Rapport_JSON"]
        if (-not $jsonPath) { continue }
        $reportDir = Split-Path $jsonPath -Parent
        if (-not (Test-Path -LiteralPath $reportDir -PathType Container)) { continue }

        $stageRel = "stage$stageNumber"
        Copy-DebugFile (Join-Path $reportDir "client_render_sampler_report.html") "$stageRel\client_render_sampler_report.html"
        Copy-DebugFile (Join-Path $reportDir "client_render_sampler_report.csv") "$stageRel\client_render_sampler_report.csv"
        if ($IncludeStageReports) {
            Copy-DebugFile (Join-Path $reportDir "client_render_sampler_report.json") "$stageRel\client_render_sampler_report.json"
        }
        Copy-DebugFile (Join-Path $reportDir "client_render_sampler_before_after.jpg") "$stageRel\client_render_sampler_before_after.jpg"
        Copy-DebugTree (Join-Path $reportDir "target_filter_archive_clean") "$stageRel\target_filter_archive_clean" -ExcludeDirNames @("rendered")
        Copy-DebugTree (Join-Path $reportDir "filter_clusters") "$stageRel\filter_clusters" -ExcludeDirNames @("rendered")

        if ($IncludeRendered) {
            Copy-DebugTree (Join-Path $reportDir "rendered") "$stageRel\rendered"
        }

        if ($IncludeLocalDb) {
            $dbPath = $paths["DB_recettes"]
            if ($dbPath) { Copy-DebugFile $dbPath "$stageRel\client_render_sampler.sqlite3" }
        }
    }

    if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }
    Compress-Archive -Path (Join-Path $TempDir "*") -DestinationPath $ZipPath -Force
    Write-Host "DEBUG ZIP READY"
    Write-Host $ZipPath
} finally {
    if (Test-Path -LiteralPath $TempDir) {
        Remove-Item -LiteralPath $TempDir -Recurse -Force
    }
}
