#!/usr/bin/env bash
# One-time setup on Raspberry Pi OS (Bookworm or newer)
set -e
cd "$(dirname "$0")"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip tmux git curl
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
mkdir -p data/logs static
if [ ! -s static/lightweight-charts.js ]; then
  curl -fsSL -o static/lightweight-charts.js \
    https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js \
    || echo "WARN: chart library download failed - dashboard will fall back to the CDN"
fi
chmod +x start.sh stop.sh update.sh
[ -f config.json ] || ./venv/bin/python -c "import tenam_core; tenam_core.load_config()"
echo
echo "Setup complete. Put your Discord webhooks into config.json (discord.errors / btc / nq), then run ./start.sh"
