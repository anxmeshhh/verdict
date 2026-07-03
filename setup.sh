#!/bin/sh
# Verdict server setup - the "<10 minutes for a stranger" path (Phase 5 gate).
# Usage: ./setup.sh
set -e

echo "verdict setup"
echo "============="

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker is not running. Install/start Docker, then re-run."
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: docker compose v2 not found."
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  DATA_DIR="$(pwd)/data"
  API_KEY="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  # portable in-place edit (BSD/GNU sed differ; use a temp file)
  sed "s|^HOST_DATA_DIR=.*|HOST_DATA_DIR=${DATA_DIR}|; s|^VERDICT_SERVER_API_KEY=.*|VERDICT_SERVER_API_KEY=${API_KEY}|" .env > .env.tmp
  mv .env.tmp .env
  echo "wrote .env  (HOST_DATA_DIR=${DATA_DIR}, generated API key)"
else
  echo ".env already exists - leaving it alone"
fi
mkdir -p data/repos data/tmp

if ! grep -q "^VERDICT_PROVIDER=." .env && ! grep -q "^VERDICT_OLLAMA_URL=." .env; then
  echo ""
  echo "NOTE: no LLM provider configured yet. Edit .env and set either:"
  echo "  VERDICT_PROVIDER + VERDICT_MODEL + VERDICT_API_KEY   (cloud - groq/openrouter/gemini/openai)"
  echo "  VERDICT_OLLAMA_URL=http://host.docker.internal:11434 (local ollama)"
fi

echo ""
echo "building and starting the stack (postgres, redis, api, worker)..."
docker compose up -d --build

echo "waiting for the API to be healthy..."
i=0
until curl -fsS http://localhost:8400/health >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo "ERROR: API never became healthy - check: docker compose logs api"
    exit 1
  fi
  sleep 2
done

echo ""
echo "verdict is up:"
echo "  run history   http://localhost:8400/        (X-API-Key: see .env)"
echo "  health        http://localhost:8400/health"
echo "  API docs      http://localhost:8400/docs"
echo ""
echo "submit a run:"
echo '  curl -X POST http://localhost:8400/runs -H "Content-Type: application/json" \'
echo '       -H "X-API-Key: <from .env>" -d "{\"repo_path\": \"/data/repos/<your-repo>\"}"'
echo ""
echo "(repos must live under ./data/repos so the sandbox can reach them)"
