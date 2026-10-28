#!/usr/bin/env bash
# Starts the paper trader (tmux "TEN") and the dashboard (tmux "TENweb", port 8757).
# Both auto-restart if they crash. Safe to run repeatedly.
cd "$(dirname "$0")"
DIR="$(pwd)"
PY="$DIR/venv/bin/python"
[ -x "$PY" ] || { echo "venv missing - run ./setup.sh first"; exit 1; }
if ! tmux has-session -t TEN 2>/dev/null; then
  tmux new-session -d -s TEN "cd '$DIR' && while true; do '$PY' tenam_trader.py; echo 'trader exited - restarting in 10s'; sleep 10; done"
  echo "started tmux session TEN (paper trader BTC + NQ)"
else echo "tmux session TEN already running"; fi
if ! tmux has-session -t TENweb 2>/dev/null; then
  tmux new-session -d -s TENweb "cd '$DIR' && while true; do '$PY' tenam_web.py; echo 'web exited - restarting in 10s'; sleep 10; done"
  echo "started tmux session TENweb (dashboard on port 8757)"
else echo "tmux session TENweb already running"; fi
