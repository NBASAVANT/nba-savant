#!/usr/bin/env python3
"""
NBA Savant Data Updater
=======================
Pulls fresh stats from NBA.com and builds the finished website.

ONE-TIME SETUP:
    python3 -m pip install nba_api pandas

TO UPDATE THE SITE (run from this folder, on your Mac):
    python3 update_nba_savant.py

The finished page is saved as index.html in this folder. Then open GitHub
Desktop, click "Commit to main", then "Push origin". Netlify publishes the
new version about a minute later.

OPTIONAL EXTRAS (type them after the command, separated by spaces):
    2025-26     build a specific season instead of the current one
    noshots     skip shot charts (finishes in under a minute, good for testing)

WHO APPEARS ON THE SITE:
    A player appears only if they have played MORE than 25% of the games
    played so far this season. The cutoff grows as the season goes on.
    The "games played so far" number is the highest GP of any player.

SAFETY:
    If NBA.com has no games yet, or the pull looks broken, the script stops
    and leaves your existing index.html untouched.
"""

import json
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
from datetime import datetime

# ───────────────────────── Settings you can change ─────────────────────────
MIN_GP_FRACTION = 0.25    # player must have played MORE than this share of games so far
MIN_LEAGUE_GAMES = 3      # don't publish until at least this many games have been played
MIN_PLAYERS = 100         # sanity check: a healthy pull has far more than this
MIN_SHOT_SUCCESS = 0.8    # at least this share of shot chart downloads must work
# ──────────────────────────────────────────────────────────────────────────

HERE = os.path.dirname(os.path.abspath(__file__))

# (NBA.com column, our key, decimals)
BASIC_COLS = [
    ('MIN', 'MP_PG', 2), ('PTS', 'PTS_PG', 2), ('REB', 'TRB_PG', 2),
    ('AST', 'AST_PG', 2), ('STL', 'STL_PG', 2), ('BLK', 'BLK_PG', 2),
    ('TOV', 'TOV_PG', 2), ('OREB', 'ORB_PG', 2), ('DREB', 'DRB_PG', 2),
    ('FGA', 'FGA_PG', 2), ('FG3A', '3PA_PG', 2), ('FTA', 'FTA_PG', 2),
    ('PLUS_MINUS', 'PM', 1),
    ('FG_PCT', 'FG%', 3), ('FG3_PCT', '3P%', 3), ('FT_PCT', 'FT%', 3),
]
# Percent stats stay as fractions (0.612). The page multiplies by 100 to show them.
ADV_COLS = [
    ('EFG_PCT', 'eFG%', 3), ('TS_PCT', 'TS%', 3), ('USG_PCT', 'USG%', 3),
    ('AST_PCT', 'AST%', 3), ('OFF_RATING', 'ORTG', 1), ('DEF_RATING', 'DRTG', 1),
    ('NET_RATING', 'NET_RTG', 1), ('PACE', 'PACE', 1), ('AST_TO', 'AST_TO', 2),
]

LG_BINS = 36  # shot distance bins, 1 foot each, last bin = 35 ft and beyond


# ───────────────────────── Small helpers ─────────────────────────

def current_season(now=None):
    """NBA seasons start in October, so Oct-Dec belongs to the season starting that year."""
    now = now or datetime.now()
    start = now.year if now.month >= 10 else now.year - 1
    return '%d-%s' % (start, str(start + 1)[2:])


def to_ascii(s):
    return ''.join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))


def num(val, decimals):
    """Clean number or None. decimals=0 returns an int."""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    r = round(f, decimals)
    return int(r) if decimals == 0 else r


def with_retries(fn, label, attempts=5, base_wait=20, timeout=90, quiet=False):
    """NBA.com is flaky. Try again with a growing pause before giving up."""
    for i in range(1, attempts + 1):
        try:
            return fn(timeout)
        except Exception as e:  # noqa: BLE001 (network errors come in many shapes)
            if i == attempts:
                if not quiet:
                    print('  ERROR: %s failed after %d attempts: %s' % (label, attempts, e))
                raise
            wait = base_wait * i
            if not quiet:
                print('       attempt %d/%d failed, retrying in %ds...' % (i, attempts, wait))
            time.sleep(wait)


def is_three(x, y):
    """Same rule the web page uses, so league averages and player charts agree."""
    return (abs(x) >= 220 and y <= 90) or math.hypot(x, y) >= 237.5


def dist_bin(x, y):
    return min(LG_BINS - 1, int(math.hypot(x, y) / 10))


# ───────────────────────── NBA.com pulls (replaced in tests) ─────────────────────────

def fetch_player_stats(season, advanced=False):
    from nba_api.stats.endpoints import leaguedashplayerstats
    label = 'advanced stats' if advanced else 'basic stats'

    def call(t):
        kw = dict(season=season, per_mode_detailed='PerGame',
                  season_type_all_star='Regular Season', timeout=t)
        if advanced:
            kw['measure_type_detailed_defense'] = 'Advanced'
        return leaguedashplayerstats.LeagueDashPlayerStats(**kw).get_data_frames()[0]

    return with_retries(call, label)


def fetch_shots(pid, season):
    from nba_api.stats.endpoints import shotchartdetail

    def call(t):
        return shotchartdetail.ShotChartDetail(
            team_id=0, player_id=pid, context_measure_simple='FGA',
            season_nullable=season, season_type_all_star='Regular Season',
            timeout=t).get_data_frames()[0]

    df = with_retries(call, 'shots %s' % pid, attempts=3, base_wait=4, timeout=45, quiet=True)
    return [[int(x), int(y), 1 if m == 1 else 0]
            for x, y, m in zip(df['LOC_X'], df['LOC_Y'], df['SHOT_MADE_FLAG'])]


# ───────────────────────── Building the data ─────────────────────────

def build_players(df_basic, df_adv):
    """Merge basic + advanced tables into one clean dict per player."""
    df = df_basic
    if df_adv is not None and len(df_adv):
        cols = ['PLAYER_ID'] + [c for c, _, _ in ADV_COLS if c in df_adv.columns]
        df = df_basic.merge(df_adv[cols].drop_duplicates('PLAYER_ID'), on='PLAYER_ID', how='left')

    players, seen = [], set()
    for rec in df.to_dict('records'):
        pid = num(rec.get('PLAYER_ID'), 0)
        gp = num(rec.get('GP'), 0)
        if not pid or not gp or pid in seen:
            continue
        seen.add(pid)
        p = {
            'Player': to_ascii(str(rec.get('PLAYER_NAME', '')).strip()),
            'nba_id': pid,
            'Team': str(rec.get('TEAM_ABBREVIATION') or ''),
            'G': gp,
        }
        age = num(rec.get('AGE'), 0)
        if age is not None:
            p['Age'] = age
        for src, key, dec in BASIC_COLS + ADV_COLS:
            v = num(rec.get(src), dec)
            if v is not None:
                p[key] = v
        players.append(p)
    return players


def pick_eligible(players):
    """Keep players who played MORE than 25% of the games played so far.

    Returns (eligible, games_so_far, min_games_needed).
    """
    games_so_far = max((p['G'] for p in players), default=0)
    cutoff = MIN_GP_FRACTION * games_so_far
    eligible = [p for p in players if p['G'] > cutoff]
    min_needed = int(math.floor(cutoff)) + 1
    return eligible, games_so_far, min_needed


def league_curve(shot_data):
    """League FG% by distance (2s and 3s separately), built from the shots we downloaded."""
    att = {0: [0] * LG_BINS, 1: [0] * LG_BINS}
    made = {0: [0] * LG_BINS, 1: [0] * LG_BINS}
    for shots in shot_data.values():
        for x, y, m in shots:
            t = 1 if is_three(x, y) else 0
            b = dist_bin(x, y)
            att[t][b] += 1
            made[t][b] += m
    out = {}
    for t, name in ((0, '2'), (1, '3')):
        total_att = sum(att[t])
        overall = (sum(made[t]) / total_att) if total_att else 0.4
        out[name] = [round(made[t][b] / att[t][b], 3) if att[t][b] >= 40 else round(overall, 3)
                     for b in range(LG_BINS)]
    return out


def shot_cache_path(season):
    return os.path.join(tempfile.gettempdir(),
                        'nba_savant_shots_%s_%s.json' % (season, datetime.now().strftime('%Y%m%d')))


def load_shot_cache(season):
    """If a run died halfway today, pick up where it left off."""
    try:
        with open(shot_cache_path(season), 'r') as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_shot_cache(season, data):
    try:
        with open(shot_cache_path(season), 'w') as f:
            json.dump(data, f, separators=(',', ':'))
    except Exception:  # noqa: BLE001
        pass


def download_shots(players, season):
    cache = load_shot_cache(season)
    shot_data = {pid: s for pid, s in cache.items() if s}
    if shot_data:
        print('       (resuming: %d shot charts already saved from earlier today)' % len(shot_data))
    total, failed = len(players), 0
    for idx, p in enumerate(players):
        key = str(p['nba_id'])
        if key in shot_data:
            continue
        try:
            shots = fetch_shots(p['nba_id'], season)
            if shots:
                shot_data[key] = shots
            time.sleep(0.6)
        except Exception:  # noqa: BLE001
            failed += 1
            time.sleep(2)
        if (idx + 1) % 25 == 0:
            print('       ... %d/%d players' % (idx + 1, total))
            save_shot_cache(season, shot_data)
    save_shot_cache(season, shot_data)
    return shot_data, failed


# ───────────────────────── Writing the page ─────────────────────────

def js_json(obj):
    """JSON that is safe to paste inside a <script> tag."""
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=True).replace('</', '<\\/')


def render_page(template, players, shot_data, meta, season):
    out = (template
           .replace('__DATA_PLACEHOLDER__', js_json(players))
           .replace('__SHOTS_PLACEHOLDER__', js_json(shot_data))
           .replace('__META_PLACEHOLDER__', js_json(meta))
           .replace('__SEASON_PLACEHOLDER__', season))
    left = re.findall(r'__[A-Z_]+_PLACEHOLDER__', out)
    if left:
        raise RuntimeError('Template still has unfilled placeholders: %s' % sorted(set(left)))
    return out


def write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


# ───────────────────────── Main ─────────────────────────

def main(argv):
    season = current_season()
    skip_shots = False
    for a in argv:
        if re.match(r'^\d{4}-\d{2}$', a):
            season = a
        elif a.lower() == 'noshots':
            skip_shots = True
        else:
            print('Unknown option: %s' % a)
            return 1

    try:
        import pandas  # noqa: F401
        import nba_api  # noqa: F401
    except ImportError:
        print('ERROR: Missing required packages. Run this first:')
        print('  python3 -m pip install nba_api pandas')
        return 1

    print()
    print('=' * 54)
    print('  NBA SAVANT  |  Data Updater  |  %s season' % season)
    print('=' * 54)
    print()

    # 1. Basic stats
    print('[1/4] Pulling per-game stats from NBA.com...')
    try:
        df_basic = fetch_player_stats(season)
    except Exception:  # noqa: BLE001
        print('  Could not reach NBA.com. This only works from your own computer')
        print('  (not GitHub). Check your internet and try again in a few minutes.')
        print('  Your site was NOT changed.')
        return 1

    if df_basic is None or len(df_basic) == 0:
        print('  NBA.com has no %s games yet, so there is nothing to publish.' % season)
        print('  Your site was NOT changed. Try again after the first games are played.')
        return 0

    players_all = build_players(df_basic, None)
    eligible, games_so_far, min_needed = pick_eligible(players_all)
    print('       %d players have played at least one game.' % len(players_all))

    if games_so_far < MIN_LEAGUE_GAMES:
        print('  The most games anyone has played is %d. That is too early for' % games_so_far)
        print('  meaningful percentiles (need %d). Your site was NOT changed.' % MIN_LEAGUE_GAMES)
        return 0
    if len(eligible) < MIN_PLAYERS:
        print('  Only %d players qualify, which looks wrong (need %d).' % (len(eligible), MIN_PLAYERS))
        print('  Your site was NOT changed.')
        return 1
    print('       Rule: more than %d%% of %d games = at least %d games.'
          % (int(MIN_GP_FRACTION * 100), games_so_far, min_needed))
    print('       %d players qualify.' % len(eligible))

    # 2. Advanced stats
    time.sleep(3)
    print('[2/4] Pulling advanced stats from NBA.com...')
    try:
        df_adv = fetch_player_stats(season, advanced=True)
    except Exception:  # noqa: BLE001
        print('  Advanced stats failed. Your site was NOT changed.')
        return 1

    # 3. Merge and filter
    print('[3/4] Building player database...')
    players_all = build_players(df_basic, df_adv)
    eligible, games_so_far, min_needed = pick_eligible(players_all)
    eligible.sort(key=lambda p: p['Player'])
    print('       %d players on the site.' % len(eligible))

    # 4. Shot charts
    shot_data, lg = {}, None
    if skip_shots:
        print('[4/4] Skipping shot charts (noshots).')
    else:
        print('[4/4] Pulling shot charts (this is the slow part, 10 to 20 minutes)...')
        shot_data, failed = download_shots(eligible, season)
        print('       Shot charts for %d players (%d failed)' % (len(shot_data), failed))
        if len(shot_data) < len(eligible) * MIN_SHOT_SUCCESS:
            print('  Too many shot chart downloads failed. NBA.com is probably slowing')
            print('  this connection down. Wait a few minutes and run the script again.')
            print('  It will pick up where it left off. Your site was NOT changed.')
            return 1
        lg = league_curve(shot_data)

    now = datetime.now()
    meta = {
        'season': season,
        'updated': '%s %d, %d' % (now.strftime('%b'), now.day, now.year),
        'gamesSoFar': games_so_far,
        'minGP': min_needed,
        'pct': int(MIN_GP_FRACTION * 100),
        'lg': lg,
    }

    with open(os.path.join(HERE, 'template.html'), 'r', encoding='utf-8') as f:
        template = f.read()
    html = render_page(template, eligible, shot_data, meta, season)

    out_path = os.path.join(HERE, 'index.html')
    write_atomic(out_path, html)

    print()
    print('=' * 54)
    print('  SUCCESS. index.html was rebuilt.')
    print('  Season:    %s' % season)
    print('  Players:   %d (%d+ games each)' % (len(eligible), min_needed))
    print('  File size: %.0f KB' % (os.path.getsize(out_path) / 1024.0))
    print()
    print('  NEXT: open GitHub Desktop, click "Commit to main",')
    print('        then click "Push origin".')
    print('=' * 54)
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
