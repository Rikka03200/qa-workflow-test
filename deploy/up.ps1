$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

python init-local-env.py

docker compose --env-file env/local.env --profile worker up -d --build

docker compose --env-file env/local.env --profile worker ps

$port = if ($env:QA_WEBAPP_PORT) { $env:QA_WEBAPP_PORT } else { "8800" }
Write-Host ""
Write-Host "qa-workflow is starting."
Write-Host "Open:   http://127.0.0.1:$port"
Write-Host "Health: http://127.0.0.1:$port/healthz"
Write-Host ""
Write-Host "Knowledge base bootstrap runs in the kb-migrate service before web/worker start."
Write-Host "To create the first user inside the web container, run:"
Write-Host "  docker compose --env-file env/local.env exec web python -m webapp.auth adduser <username> --name <display-name> --role admin"
