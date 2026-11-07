#!/bin/bash
# Double-click (or ask Claude) to update NBA Savant now: pulls fresh stats,
# rebuilds the site and pushes it to GitHub so Cloudflare publishes it.
cd "$(dirname "$0")" || exit 1
STATUS="automation/last_update_status.txt"
echo "RUNNING $(date)" > "$STATUS"
if bash automation/auto_update.sh; then
  echo "DONE $(date)" > "$STATUS"
  echo; echo ">>> NBA Savant is updated. You can close this window."
else
  echo "FAILED $(date)" > "$STATUS"
  echo; echo ">>> Update FAILED. Copy the lines above and send them to Claude."
fi
