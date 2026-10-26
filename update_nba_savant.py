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
    restyle     re-apply template.html to the current index.html without
                downloading anything (for design changes; takes a second)

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
from datetime import datetime, timedelta

# ───────────────────────── Settings you can change ─────────────────────────
MIN_GP_FRACTION = 0.25    # player must have played MORE than this share of games so far
MIN_LEAGUE_GAMES = 1      # don't publish until at least this many games have been played
MIN_PLAYERS = 20          # sanity check (opening night only has a few teams' players)
MIN_SHOT_SUCCESS = 0.8    # at least this share of shot chart downloads must work
MOVER_WINDOW_DAYS = 14    # Rising/Falling compares today with this many days ago
MOVER_MIN_GAMES_THEN = 4  # the "then" snapshot needs this many games, else compare with last season
MOVER_MIN_MINUTES = 15    # Rising/Falling only lists rotation players (minutes per game)
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

# Stats used for Rising/Falling: (data key, label, higher is better, qualifier)
MOVER_STATS = [
    ('PTS_PG', 'PTS', True, None), ('TRB_PG', 'REB', True, None), ('AST_PG', 'AST', True, None),
    ('STL_PG', 'STL', True, None), ('BLK_PG', 'BLK', True, None), ('FG%', 'FG%', True, 'shoot'),
    ('3P%', '3P%', True, 'three'), ('FT%', 'FT%', True, 'ft'), ('TOV_PG', 'TOV', False, None),
]
# Must match Q in template.html so percentiles agree with the player pages.
QUALIFIERS = {
    'shoot': lambda p: (p.get('FGA_PG') or 0) >= 3,
    'three': lambda p: (p.get('3PA_PG') or 0) >= 1.5,
    'ft': lambda p: (p.get('FTA_PG') or 0) >= 1,
}


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

def fetch_player_stats(season, advanced=False, date_to=None):
    """date_to (a date) returns the stats as they stood at the end of that day."""
    from nba_api.stats.endpoints import leaguedashplayerstats
    label = 'advanced stats' if advanced else 'basic stats'

    def call(t):
        kw = dict(season=season, per_mode_detailed='PerGame',
                  season_type_all_star='Regular Season', timeout=t)
        if advanced:
            kw['measure_type_detailed_defense'] = 'Advanced'
        if date_to is not None:
            kw['date_to_nullable'] = date_to.strftime('%m/%d/%Y')
        return leaguedashplayerstats.LeagueDashPlayerStats(**kw).get_data_frames()[0]

    return with_retries(call, label)


def fetch_game_logs(season):
    """Every player's game-by-game box score for the season, in one request."""
    from nba_api.stats.endpoints import leaguegamelog

    def call(t):
        return leaguegamelog.LeagueGameLog(
            season=season, season_type_all_star='Regular Season',
            player_or_team_abbreviation='P', timeout=t).get_data_frames()[0]

    return with_retries(call, 'game logs', attempts=3, base_wait=10)


def fetch_bio(season):
    """Position, height, weight, jersey, college and draft for every player. Optional:
    if NBA.com refuses, the site still builds without these details."""
    from nba_api.stats.endpoints import playerindex

    def call(t):
        return playerindex.PlayerIndex(season=season, league_id='00', timeout=t).get_data_frames()[0]

    try:
        df = with_retries(call, 'player bios', attempts=3, base_wait=10)
    except Exception as e:  # noqa: BLE001
        print('       (Skipped player bios: %s)' % e)
        return {}
    bio = {}
    for rec in df.to_dict('records'):
        pid = num(rec.get('PERSON_ID'), 0)
        if not pid:
            continue
        b = {}
        for src, key in (('POSITION', 'Pos'), ('HEIGHT', 'Ht'), ('JERSEY_NUMBER', 'No'),
                         ('COLLEGE', 'College'), ('COUNTRY', 'Country')):
            v = rec.get(src)
            if v is not None and str(v).strip() not in ('', 'nan', 'None'):
                b[key] = to_ascii(str(v).strip())
        wt = num(rec.get('WEIGHT'), 0)
        if wt:
            b['Wt'] = wt
        try:
            yr = int(float(rec.get('DRAFT_YEAR')))
            try:
                b['Draft'] = '%d R%d #%d' % (yr, int(float(rec.get('DRAFT_ROUND'))), int(float(rec.get('DRAFT_NUMBER'))))
            except (TypeError, ValueError):
                b['Draft'] = '%d Undrafted' % yr
        except (TypeError, ValueError):
            pass
        if b:
            bio[pid] = b
    return bio


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
        tid = num(rec.get('TEAM_ID'), 0)
        if tid:
            p['tid'] = tid
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


def previous_season(season):
    start = int(season[:4]) - 1
    return '%d-%s' % (start, str(start + 1)[2:])


def fmt_date(d):
    return '%s %d, %d' % (d.strftime('%b'), d.day, d.year)


def percentiles(pool, key, higher, qual):
    """Percentile (1-99) for each player, same formula as the web page."""
    vals = [(p['nba_id'], p[key]) for p in pool
            if p.get(key) is not None and (qual is None or qual(p))]
    nums = [v for _, v in vals]
    n = len(nums)
    out = {}
    for pid, v in vals:
        below = sum(1 for x in nums if (x < v if higher else x > v))
        same = sum(1 for x in nums if x == v)
        out[pid] = max(1, min(99, int(math.floor((below + same / 2.0) / n * 100 + 0.5))))
    return out


def compute_movers(now_pool, then_pool, limit=15):
    """Each player's single biggest percentile jump (and drop) between two snapshots."""
    best_up, best_down = {}, {}
    for key, label, higher, qname in MOVER_STATS:
        qual = QUALIFIERS.get(qname)
        then_p = percentiles(then_pool, key, higher, qual)
        now_p = percentiles(now_pool, key, higher, qual)
        for p in now_pool:
            pid = p['nba_id']
            if pid not in then_p or pid not in now_p:
                continue
            if (p.get('MP_PG') or 0) < MOVER_MIN_MINUTES:
                continue
            d = now_p[pid] - then_p[pid]
            rec = {'id': pid, 'n': p['Player'], 't': p['Team'], 's': label,
                   'from': then_p[pid], 'to': now_p[pid], 'd': d}
            if d > 0 and (pid not in best_up or d > best_up[pid]['d']):
                best_up[pid] = rec
            if d < 0 and (pid not in best_down or d < best_down[pid]['d']):
                best_down[pid] = rec
    rising = sorted(best_up.values(), key=lambda r: (-r['d'], r['n']))[:limit]
    falling = sorted(best_down.values(), key=lambda r: (r['d'], r['n']))[:limit]
    return rising, falling


def build_movers(season, now_pool, today):
    """Rising/Falling without saved history.

    NBA.com can return stats "as of" any past date, so we rebuild what the
    percentiles were 14 days ago. Early in the season (when 14 days ago is
    before enough games were played) we compare with last season instead.
    """
    then_date = today - timedelta(days=MOVER_WINDOW_DAYS)
    then_pool, then_games = [], 0
    try:
        df = fetch_player_stats(season, date_to=then_date)
        if df is not None and len(df):
            then_pool, then_games, _ = pick_eligible(build_players(df, None))
    except Exception:  # noqa: BLE001
        then_pool = []
    if then_pool and then_games >= MOVER_MIN_GAMES_THEN:
        mode, since = 'window', fmt_date(then_date)
    else:
        prev = previous_season(season)
        try:
            df = fetch_player_stats(prev)
        except Exception:  # noqa: BLE001
            return None
        if df is None or not len(df):
            return None
        then_pool = pick_eligible(build_players(df, None))[0]
        mode, since = 'lastseason', prev
    rising, falling = compute_movers(now_pool, then_pool)
    if not rising and not falling:
        return None
    return {'mode': mode, 'days': MOVER_WINDOW_DAYS, 'since': since,
            'rising': rising, 'falling': falling}


def _minutes(v):
    """Minutes can arrive as 34, 34.5 or '34:12'."""
    if isinstance(v, str) and ':' in v:
        mm, ss = v.split(':', 1)
        return round(int(mm) + int(ss) / 60.0)
    n = num(v, 0)
    return n if n is not None else 0


def build_game_logs(df, player_ids):
    """{player id: [[date, opp, W/L, MIN, PTS, REB, AST, STL, BLK, TOV, FGM, FGA,
    3PM, 3PA, FTM, FTA, OREB, DREB, PF, +/-], ...]} oldest game first."""
    logs = {}
    if df is None or not len(df):
        return logs
    for rec in df.to_dict('records'):
        pid = num(rec.get('PLAYER_ID'), 0)
        if pid not in player_ids:
            continue
        matchup = str(rec.get('MATCHUP') or '')
        opp = ('@ ' if ' @ ' in matchup else 'vs ') + matchup.split(' ')[-1]
        date = str(rec.get('GAME_DATE') or '')[:10]
        row = [date, opp, str(rec.get('WL') or '')[:1], _minutes(rec.get('MIN'))]
        for col in ('PTS', 'REB', 'AST', 'STL', 'BLK', 'TOV', 'FGM', 'FGA', 'FG3M', 'FG3A',
                    'FTM', 'FTA', 'OREB', 'DREB', 'PF', 'PLUS_MINUS'):
            v = num(rec.get(col), 0)
            row.append(v if v is not None else 0)
        logs.setdefault(str(pid), []).append(row)
    for games in logs.values():
        games.sort(key=lambda g: g[0])
        for g in games:   # store dates compactly as M/D for display
            try:
                g[0] = '%d/%d' % (int(g[0][5:7]), int(g[0][8:10]))
            except ValueError:
                pass
    return logs


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


def render_page(template, players, shot_data, meta, season, game_logs=None):
    out = (template
           .replace('__DATA_PLACEHOLDER__', js_json(players))
           .replace('__SHOTS_PLACEHOLDER__', js_json(shot_data))
           .replace('__LOGS_PLACEHOLDER__', js_json(game_logs or {}))
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


def restyle():
    """Rebuild index.html from its own embedded data with the latest template.html."""
    path = os.path.join(HERE, 'index.html')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            old = f.read()
        grab = lambda name, nxt: re.search(r'var %s=(.*?);\n(?:var %s=)' % (name, nxt), old, re.S).group(1)
        players = json.loads(grab('D', 'SD'))
        shots = json.loads(grab('SD', 'META'))
        meta = json.loads(re.search(r'var META=(\{.*?\});\n', old, re.S).group(1))
        m = re.search(r'var GL=(\{.*?\});\n', old, re.S)
        logs = json.loads(m.group(1)) if m else {}
    except Exception as e:  # noqa: BLE001
        print('Could not read the data inside index.html (%s).' % e)
        print('Run the normal update instead. Your site was NOT changed.')
        return 1
    with open(os.path.join(HERE, 'template.html'), 'r', encoding='utf-8') as f:
        template = f.read()
    write_atomic(path, render_page(template, players, shots, meta, meta.get('season', ''), logs))
    print('Restyled index.html (%d players, data from %s).' % (len(players), meta.get('updated')))
    print('NEXT: open GitHub Desktop, click "Commit to main", then "Push origin".')
    return 0


# ───────────────────────── Main ─────────────────────────

def main(argv):
    season = current_season()
    skip_shots = False
    if any(a.lower() == 'restyle' for a in argv):
        return restyle()
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
    print('[1/5] Pulling per-game stats from NBA.com...')
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
    print('[2/5] Pulling advanced stats from NBA.com...')
    try:
        df_adv = fetch_player_stats(season, advanced=True)
    except Exception:  # noqa: BLE001
        print('  Advanced stats failed. Your site was NOT changed.')
        return 1

    # 3. Merge and filter
    print('[3/5] Building player database...')
    players_all = build_players(df_basic, df_adv)
    eligible, games_so_far, min_needed = pick_eligible(players_all)
    bio = fetch_bio(season)
    for p in eligible:
        p.update(bio.get(p['nba_id'], {}))
    eligible.sort(key=lambda p: p['Player'])
    print('       %d players on the site.' % len(eligible))

    # 4. Rising / Falling
    today = datetime.now().date()
    if season != current_season():   # an old season: measure from its final weeks
        today = min(today, datetime(int(season[:4]) + 1, 4, 14).date())
    print('[4/5] Working out who is rising and falling...')
    movers = build_movers(season, eligible, today)
    if movers:
        print('       Compared with %s.' % ('last season' if movers['mode'] == 'lastseason'
                                           else movers['since']))
    else:
        print('       Skipped (not enough data yet). The site will hide that section.')

    # Game logs (one request). If it fails the site still builds without them.
    game_logs = {}
    try:
        game_logs = build_game_logs(fetch_game_logs(season), {p['nba_id'] for p in eligible})
        print('       Game logs for %d players.' % len(game_logs))
    except Exception:  # noqa: BLE001
        print('       Game logs skipped (NBA.com did not answer). Site still builds.')

    # 5. Shot charts
    shot_data, lg = {}, None
    if skip_shots:
        print('[5/5] Skipping shot charts (noshots).')
    else:
        print('[5/5] Pulling shot charts (this is the slow part, 10 to 20 minutes)...')
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
        'movers': movers,
    }

    with open(os.path.join(HERE, 'template.html'), 'r', encoding='utf-8') as f:
        template = f.read()
    html = render_page(template, eligible, shot_data, meta, season, game_logs)

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
