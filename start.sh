#!/usr/bin/env bash
# Start the Global Context Console. The console starts and stops the voice agent itself.
#
#   ./start.sh              http://localhost:8000
#   PORT=8080 ./start.sh    another port for the console (the voice agent always uses 7860)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$ROOT/app"
PORT="${PORT:-8000}"

[ -x "$APP/.venv/bin/python" ] || { echo "Not set up yet. Run ./setup.sh first."; exit 1; }
[ -f "$APP/.env" ] || { echo "app/.env missing. Run ./setup.sh first."; exit 1; }
grep -qE '^SARVAM_API_KEY=.+' "$APP/.env" || { echo "SARVAM_API_KEY is empty in app/.env. Add it and retry."; exit 1; }

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
if port_busy "$PORT"; then
  echo "Port $PORT is already in use (is the console already running?). Use: PORT=8080 ./start.sh"; exit 1
fi
if port_busy 7860; then
  echo "Warning: port 7860 is in use. Stop any voice_agent.py started by hand, or calls will fail."
fi

echo "Global Context Console → http://localhost:$PORT   (Ctrl+C to stop)"
cd "$APP"
exec "$APP/.venv/bin/python" -m uvicorn console:app --host 127.0.0.1 --port "$PORT"
