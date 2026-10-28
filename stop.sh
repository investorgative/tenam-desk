#!/usr/bin/env bash
tmux kill-session -t TEN 2>/dev/null && echo "stopped TEN"
tmux kill-session -t TENweb 2>/dev/null && echo "stopped TENweb"
true
