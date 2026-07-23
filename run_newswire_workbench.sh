#!/bin/bash
set -euo pipefail

REPO_DIR="/Users/kevinmahoney/Library/Mobile Documents/com~apple~CloudDocs/source-intelligence"
PUBLISHER_ENV="/Users/kevinmahoney/marketingbykevin-publisher/.env"
WORKBENCH_ENV="$HOME/.source-intelligence/newswire-workbench.env"

if [ -f "$PUBLISHER_ENV" ]; then
  # shellcheck disable=SC1090
  source "$PUBLISHER_ENV"
fi
if [ -f "$WORKBENCH_ENV" ]; then
  # shellcheck disable=SC1090
  source "$WORKBENCH_ENV"
fi

cd "$REPO_DIR"
exec python3 -m streamlit run newswire_workbench_app.py \
  --server.address 127.0.0.1 \
  --server.port 8101 \
  --browser.gatherUsageStats false
