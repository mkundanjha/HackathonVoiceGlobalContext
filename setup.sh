#!/usr/bin/env bash
# One-time setup: Python environment, dependencies and app/.env.
# Safe to run again: it reuses the existing environment and never overwrites a filled-in value.
#
#   ./setup.sh                    use python3
#   PYTHON=python3.12 ./setup.sh  use a specific Python (3.11 or newer)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$ROOT/app"
VENV="$APP/.venv"
ENV_FILE="$APP/.env"
KEY_FILE="$ROOT/sarvam ai details/sarvam api key"

step() { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$1"; }
fail() { printf '    \033[31m✗\033[0m %s\n' "$1"; exit 1; }

# Value of KEY in .env (empty if missing).
env_get() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true; }
# Set KEY=VALUE in .env without printing the value.
env_set() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  if grep -qE "^$key=" "$ENV_FILE"; then
    awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k {print k"="v; next} {print}' "$ENV_FILE" > "$tmp"
  else
    cat "$ENV_FILE" > "$tmp"; printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  mv "$tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
}

step "Checking Python"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || fail "$PY not found. Install Python 3.11+ or run: PYTHON=/path/to/python ./setup.sh"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || fail "$("$PY" --version) is too old; need 3.11+. Run: PYTHON=python3.12 ./setup.sh"
"$PY" -c 'import venv, ensurepip' 2>/dev/null \
  || fail "Python venv module missing. On Ubuntu: sudo apt install python3-venv (or python3.12-venv)"
ok "$("$PY" --version)"

step "Creating the virtual environment (app/.venv)"
if [ -x "$VENV/bin/python" ]; then
  ok "already exists, reusing it"
else
  "$PY" -m venv "$VENV"
  ok "created"
fi

step "Installing dependencies (first run takes a few minutes)"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$APP/requirements.txt"
"$VENV/bin/python" -c 'import pipecat, sarvamai, fastapi, uvicorn, httpx, yaml, dotenv, loguru' \
  || fail "packages installed but cannot be imported; see the pip output above"
ok "pipecat-ai $("$VENV/bin/python" -c 'from importlib.metadata import version as v; print(v("pipecat-ai"))'), sarvamai $("$VENV/bin/python" -c 'from importlib.metadata import version as v; print(v("sarvamai"))')"

step "Configuring app/.env"
if [ -f "$ENV_FILE" ]; then
  ok "app/.env exists, keeping your values"
else
  cp "$APP/.env.example" "$ENV_FILE"; chmod 600 "$ENV_FILE"
  ok "created app/.env from .env.example"
fi

if [ -z "$(env_get SARVAM_API_KEY)" ]; then
  if [ -f "$KEY_FILE" ]; then
    env_set SARVAM_API_KEY "$(tr -d '[:space:]' < "$KEY_FILE")"
    ok "SARVAM_API_KEY filled from 'sarvam ai details/sarvam api key'"
  elif [ -t 0 ]; then
    read -r -s -p "    Sarvam API key (input hidden, Enter to skip): " key; echo
    [ -n "$key" ] && env_set SARVAM_API_KEY "$key" && ok "SARVAM_API_KEY saved"
  fi
fi
[ -n "$(env_get SARVAM_API_KEY)" ] && ok "SARVAM_API_KEY is set" \
  || warn "SARVAM_API_KEY is empty: add it to app/.env before starting (required)"

if [ -z "$(env_get GC_AK)" ] || [ -z "$(env_get GC_EMPID)" ]; then
  warn "Context API not configured (GC_EMPID / GC_AK in app/.env). Optional: without it,"
  warn "context files are built from the local datasets only. GC_AK expires daily; refresh it there."
else
  ok "Context API credentials present (GC_AK expires daily; refresh it in app/.env)"
fi

step "Checking data folders"
[ -d "$ROOT/datasets/Global Context - Seller Dataset" ] && ok "datasets/Global Context - Seller Dataset" \
  || warn "datasets/Global Context - Seller Dataset not found: dataset history will be empty"
[ -f "$ROOT/buyer.schema.md" ] && [ -f "$ROOT/seller.schema.md" ] && ok "buyer.schema.md and seller.schema.md" \
  || warn "buyer.schema.md / seller.schema.md missing: needed to generate context files"
mkdir -p "$ROOT/memory" "$ROOT/contexts"
ok "memory/ and contexts/ ready"

chmod +x "$ROOT/start.sh" 2>/dev/null || true
step "Setup complete"
echo "    Start the console:  ./start.sh"
echo "    Then open:          http://localhost:8000"
