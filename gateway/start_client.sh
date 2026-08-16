#!/bin/bash
# Start the Nascence dialogue gateway and open the client in your browser
cd "$(dirname "$0")/.." || exit 1
if [ ! -f "venv/bin/python" ]; then
  echo "[ERROR] Virtual env not found: venv/"
  exit 1
fi
echo "Starting dialogue gateway on http://127.0.0.1:8899 ..."
( sleep 1; xdg-open "http://127.0.0.1:8899/" >/dev/null 2>&1 || open "http://127.0.0.1:8899/" >/dev/null 2>&1 ) &
venv/bin/python gateway/server.py --port 8899
