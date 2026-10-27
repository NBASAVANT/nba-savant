#!/bin/bash
# One-time setup: updates the site right now, then schedules the nightly 2:00 AM update.
DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.nbasavant.update"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/nba-savant-update.log"
PY="$(command -v python3)"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"; touch "$LOG"
echo "NBA Savant auto-update setup"
if ! "$PY" -c "import nba_api, pandas" 2>/dev/null; then
  echo "Installing nba_api..."; "$PY" -m pip install -q nba_api pandas || { echo "ERROR: run  $PY -m pip install nba_api pandas  then try again."; exit 1; }
fi
sed "s#__PY__#$PY#" "$DIR/automation/auto_update.template.sh" > "$DIR/automation/auto_update.sh"
chmod +x "$DIR/automation/auto_update.sh"

echo
echo "STEP 1 of 2: updating the site now (a few minutes)..."
if /bin/bash "$DIR/automation/auto_update.sh"; then UPD=ok; else UPD=fail; fi

echo
echo "STEP 2 of 2: setting up the nightly 2:00 AM update..."
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$DIR/automation/auto_update.sh</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>2</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
launchctl bootstrap "gui/$(id -u)" "$PLIST" || { echo "ERROR: couldn't schedule the job."; exit 1; }

# Background permission check: a tiny job that only tries to look inside the folder
CK="com.nbasavant.check"; CKP="$HOME/Library/LaunchAgents/$CK.plist"; CKF="$HOME/Library/Logs/nba-savant-check.txt"
rm -f "$CKF"
cat > "$CKP" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$CK</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>-c</string><string>if head -c 10 "$DIR/automation/auto_update.sh" >/dev/null 2>&amp;1 &amp;&amp; ls "$DIR" >/dev/null 2>&amp;1; then echo OK; else echo BLOCKED; fi</string></array>
  <key>StandardOutPath</key><string>$CKF</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$CK" 2>/dev/null
launchctl bootstrap "gui/$(id -u)" "$CKP"; launchctl kickstart "gui/$(id -u)/$CK"; sleep 3
launchctl bootout "gui/$(id -u)/$CK" 2>/dev/null; rm -f "$CKP"
RES="$(cat "$CKF" 2>/dev/null)"

echo
echo "================ RESULT ================"
[ "$UPD" = ok ] && echo "Site update: DONE (latest stats pushed to GitHub)" || echo "Site update: FAILED (see the messages above)"
if [ "$RES" = "OK" ]; then
  echo "Nightly 2:00 AM update: ON"
else
  echo "Nightly 2:00 AM update: scheduled, but macOS is blocking it."
  echo "One-time fix:"
  echo "  1. Open System Settings > Privacy & Security > Full Disk Access"
  echo "  2. Click +, press Cmd+Shift+G, type  /bin/bash  and press Enter, click Open"
  echo "  3. Make sure the bash switch is on"
  echo "  4. Run this same command again"
fi
