#!/usr/bin/env bash
# Sync this pipeline to a Linux node (bubblewrap + uv + an authenticated `claude`) and run it there.
# Usage: [REMOTE_HOST=host] [REMOTE_DIR=dir] [AGENT_MODEL=opus] [STEP_LIMIT=40] ./sync.sh [tasks.txt | instance_id]
set -euo pipefail
REMOTE_HOST="${REMOTE_HOST:-nsl7s}"
REMOTE_DIR="${REMOTE_DIR:-caden-pipeline}"
HERE="$(cd "$(dirname "$0")" && pwd)"
rsync -az --delete \
  --exclude '.venv' --exclude 'work' --exclude 'results' \
  --exclude '__pycache__' --exclude '.git' --exclude '*.egg-info' \
  "$HERE/" "$REMOTE_HOST:~/$REMOTE_DIR/"
ssh "$REMOTE_HOST" "cd ~/$REMOTE_DIR && AGENT_MODEL='${AGENT_MODEL:-}' STEP_LIMIT='${STEP_LIMIT:-40}' uv run --python 3.11 python pipeline.py ${1:-tasks.txt}"
