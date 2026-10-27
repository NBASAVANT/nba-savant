#!/bin/bash
# NBA Savant nightly update: pull fresh stats from NBA.com, rebuild index.html,
# then commit and push it to GitHub. Run by macOS every night at 2:00 AM.
# (The installer fills in the Python path below; edit install_auto_update.sh, not this.)
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="__PY__"
LOG="$HOME/Library/Logs/nba-savant-update.log"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
exec > >(tee -a "$LOG") 2>&1
echo
echo "===== NBA Savant update · $(date) ====="
cd "$DIR" || { echo "ERROR: can't open $DIR (see Full Disk Access tip in automation/HOW_TO.txt)"; exit 1; }
GIT_TERMINAL_PROMPT=0 git pull --rebase --autostash -q origin main || echo "(Couldn't pull from GitHub first, continuing.)"
"$PY" update_nba_savant.py || { echo "ERROR: stats update failed. index.html was left as it was."; exit 1; }
if git diff --quiet -- index.html; then echo "No new stats since last update. Nothing to push."; exit 0; fi
git add index.html
git commit -q -m "Nightly stats update $(date +%Y-%m-%d)"
if GIT_TERMINAL_PROMPT=0 git push -q origin HEAD; then
  echo "DONE: new stats pushed to GitHub."
else
  echo "ERROR: couldn't push to GitHub. Open Terminal, cd into the nba-savant folder and run: git push"
  exit 1
fi
