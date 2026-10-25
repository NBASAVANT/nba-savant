NBA SAVANT: HOW TO UPDATE THE SITE
==================================

EVERY UPDATE
  1. Open Terminal and paste this line, then press Enter:

       cd ~/Documents/GitHub/nba-savant && python3 update_nba_savant.py

  2. Wait 10 to 20 minutes with your laptop awake. It is done when you see
     the word SUCCESS.
  3. Open GitHub Desktop. Type "Update site" in the Summary box (bottom left),
     click "Commit to main", then click "Push origin" at the top.
  4. About a minute later your site (nba-savant.netlify.app) shows the new
     stats. Refresh the page.

WHY IT HAS TO BE YOUR MAC
  NBA.com blocks GitHub and other cloud computers, so the stats can only be
  downloaded from your own computer. Nothing updates by itself. The site only
  changes when you do the steps above. The GitHub robot that used to run
  daily is turned off, so you will not get failure emails.

WHEN TO UPDATE
  The 2026-27 regular season opens Oct 20, 2026. Before games exist, the
  script says "nothing to publish" and leaves your site alone, so running it
  early is harmless. Plan on the first real update a few days after opening
  night, then whenever you want fresh numbers (once a week is plenty).

WHO APPEARS ON THE SITE
  A player appears only if they have played MORE than 25% of the games played
  so far this season. The script figures out the cutoff for you:
    "games played so far" = the most games any player has played
    cutoff                = 25% of that
  Example: after 20 games the cutoff is 5, so a player needs 6 or more games.
  Example: near the end of an 82 game season a player needs 21 or more games.
  The home page shows the current cutoff in small text under the title.
  To change the 25%, edit MIN_GP_FRACTION near the top of update_nba_savant.py.

RISING / FALLING (home page)
  No saved history is needed. Each update asks NBA.com for the stats as they
  stood 14 days ago and compares percentiles. Early in the season, when 14
  days ago is before teams had played 4 games, it compares each player with
  his own percentile from last season instead (the section says "vs last
  season"). It switches to the 14 day view on its own. Only players averaging
  15+ minutes are listed.

THE FILES
  update_nba_savant.py   The updater. Downloads stats and builds the site.
  template.html          The design and page code. Edit this to change how
                         the site looks or works.
  index.html             The finished website. The updater rewrites it every
                         time. Never edit it by hand.

OPTIONAL SHORTCUTS (type after the command, separated by spaces)
  python3 update_nba_savant.py noshots
      Skips shot charts. Finishes in about a minute. Good for a quick test,
      but do not publish that version.
  python3 update_nba_savant.py 2025-26
      Builds a specific season instead of the current one.

  python3 update_nba_savant.py restyle
      Applies a new template.html (design changes) to the stats you already
      have, without downloading anything. Takes one second.

THE SCRIPT PROTECTS YOUR SITE
  It will NOT rewrite index.html when:
    - NBA.com has no games yet for the season
    - fewer than 20 players qualify
    - too many shot chart downloads fail
  In every one of those cases it prints "Your site was NOT changed".

IF SOMETHING GOES WRONG
  "Could not reach NBA.com"
      Check your internet, wait a few minutes, run it again.
  It stops partway through the shot charts
      Just run it again. It remembers what it already downloaded today and
      continues from there.
  "pip not found" or "nba_api not found"
      Run: python3 -m pip install nba_api pandas
  GitHub Desktop shows no changes after a run
      Nothing new was published (see the protections above).
