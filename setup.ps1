# Verdict server setup (Windows) - the "<10 minutes for a stranger" path.
# Usage: .\setup.ps1
$ErrorActionPreference = "Stop"

Write-Host "verdict setup"
Write-Host "============="

docker info *> $null
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: Docker is not running. Start Docker Desktop, then re-run."; exit 1 }

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    $dataDir = Join-Path (Get-Location) "data"
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $apiKey = ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
    (Get-Content .env) `
        -replace '^HOST_DATA_DIR=.*', "HOST_DATA_DIR=$dataDir" `
        -replace '^VERDICT_SERVER_API_KEY=.*', "VERDICT_SERVER_API_KEY=$apiKey" |
        Set-Content .env -Encoding utf8
    Write-Host "wrote .env  (HOST_DATA_DIR=$dataDir, generated API key)"
} else {
    Write-Host ".env already exists - leaving it alone"
}
New-Item -ItemType Directory -Force "data\repos" | Out-Null
New-Item -ItemType Directory -Force "data\tmp" | Out-Null

$env_content = Get-Content .env -Raw
if ($env_content -notmatch "(?m)^VERDICT_PROVIDER=.+" -and $env_content -notmatch "(?m)^VERDICT_OLLAMA_URL=.+") {
    Write-Host ""
    Write-Host "NOTE: no LLM provider configured yet. Edit .env and set either:"
    Write-Host "  VERDICT_PROVIDER + VERDICT_MODEL + VERDICT_API_KEY   (cloud - groq/openrouter/gemini/openai)"
    Write-Host "  VERDICT_OLLAMA_URL=http://host.docker.internal:11434 (local ollama)"
}

Write-Host ""
Write-Host "building and starting the stack (postgres, redis, api, worker)..."
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: compose failed"; exit 1 }

Write-Host "waiting for the API to be healthy..."
$ok = $false
foreach ($i in 1..60) {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:8400/health" -TimeoutSec 3 | Out-Null
        $ok = $true; break
    } catch { Start-Sleep -Seconds 2 }
}
if (-not $ok) { Write-Host "ERROR: API never became healthy - check: docker compose logs api"; exit 1 }

Write-Host ""
Write-Host "verdict is up:"
Write-Host "  run history   http://localhost:8400/        (X-API-Key: see .env)"
Write-Host "  health        http://localhost:8400/health"
Write-Host "  API docs      http://localhost:8400/docs"
Write-Host ""
Write-Host "submit a run (repos must live under .\data\repos so the sandbox can reach them):"
Write-Host '  curl -X POST http://localhost:8400/runs -H "Content-Type: application/json" -H "X-API-Key: <from .env>" -d "{\"repo_path\": \"/data/repos/<your-repo>\"}"'
