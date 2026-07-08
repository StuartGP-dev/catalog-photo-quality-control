param(
    [string]$DbName = "catalog_filter_engine",
    [string]$User = "catalog_user",
    [string]$Password = "catalog_password_change_me",
    [int]$Port = 5432
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$env:CATALOG_POSTGRES_DB = $DbName
$env:CATALOG_POSTGRES_USER = $User
$env:CATALOG_POSTGRES_PASSWORD = $Password
$env:CATALOG_POSTGRES_PORT = "$Port"

$ComposeFile = Join-Path $RepoRoot "infra\postgres\docker-compose.catalog-db.yml"

docker compose -f $ComposeFile up -d

$Dsn = "postgresql://$User:$Password@localhost:$Port/$DbName"
Write-Host ""
Write-Host "Shared DB started. For this PC, use:"
Write-Host "`$env:CATALOG_DB_DSN = `"$Dsn`""
Write-Host ""
Write-Host "For other PCs over Tailscale, replace localhost with this PC Tailscale IP."
