param(
    [string]$DbName = "catalog_filter_engine",
    [string]$User = "catalog_user",
    [string]$Password = "catalog_password_change_me",
    [int]$Port = 5432,
    [switch]$ResetData
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$Docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $Docker) {
    throw "Docker CLI not found. Install/start Docker Desktop, then rerun this script."
}

try {
    docker version | Out-Null
} catch {
    throw "Docker Desktop is not running or the Docker engine is unavailable. Start Docker Desktop, wait until it says running, then rerun this script."
}

$env:CATALOG_POSTGRES_DB = $DbName
$env:CATALOG_POSTGRES_USER = $User
$env:CATALOG_POSTGRES_PASSWORD = $Password
$env:CATALOG_POSTGRES_PORT = "$Port"

$ComposeFile = Join-Path $RepoRoot "infra\postgres\docker-compose.catalog-db.yml"

if ($ResetData) {
    Write-Host "ResetData enabled: stopping container and removing the catalog PostgreSQL volume."
    docker compose -f $ComposeFile down -v
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose down -v failed with exit code $LASTEXITCODE."
    }
}

docker compose -f $ComposeFile up -d
if ($LASTEXITCODE -ne 0) {
    throw "docker compose failed with exit code $LASTEXITCODE. Shared DB was not started."
}

# Use ${...} because PowerShell parses "$User:" as a scoped variable reference.
$Dsn = "postgresql://${User}:${Password}@localhost:${Port}/${DbName}"
Write-Host ""
Write-Host "Shared DB started. For this PC, use:"
Write-Host "`$env:CATALOG_DB_DSN = `"$Dsn`""
Write-Host ""
Write-Host "For other PCs over Tailscale, replace localhost with this PC Tailscale IP."
