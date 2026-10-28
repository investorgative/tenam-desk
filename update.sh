#!/usr/bin/env bash
# Pull the newest version from GitHub and restart (keeps config.json, data/ and your paper accounts).
cd "$(dirname "$0")"
cp config.json /tmp/tenam_config_backup.json 2>/dev/null
git pull --ff-only && ./venv/bin/pip install -q -r requirements.txt
./stop.sh && sleep 1 && ./start.sh
