#!/bin/bash
# Removes the 2:00 AM scheduled update for good.
cd "$(dirname "$0")" || exit 1
launchctl bootout "gui/$(id -u)/com.nbasavant.update" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/com.nbasavant.update.plist"
if launchctl print "gui/$(id -u)/com.nbasavant.update" >/dev/null 2>&1 || [ -f "$HOME/Library/LaunchAgents/com.nbasavant.update.plist" ]; then
  echo "STILL ON $(date)" > autoupdate_off_status.txt; echo ">>> Could not turn it off. Tell Claude."
else
  echo "OFF $(date)" > autoupdate_off_status.txt; echo ">>> The nightly 2 AM update is OFF. You can close this window."
fi
