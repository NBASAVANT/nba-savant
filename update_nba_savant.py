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
    def call(t):
        from nba_api.stats.endpoints import playerindex
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


DEF_RANGES = {
    'open': ['4-6 Feet - Open', '6+ Feet - Wide Open'],
    'tight': ['0-2 Feet - Very Tight', '2-4 Feet - Tight'],
}


def fetch_pt_shots(season, dist_range):
    """Every player's shooting with the closest defender in one distance range (season totals)."""
    from nba_api.stats.endpoints import leaguedashplayerptshot

    def call(t):
        return leaguedashplayerptshot.LeagueDashPlayerPtShot(
            season=season, per_mode_simple='Totals', season_type_all_star='Regular Season',
            close_def_dist_range_nullable=dist_range, timeout=t).get_data_frames()[0]

    return with_retries(call, 'shots (%s)' % dist_range, attempts=3, base_wait=10, timeout=60)


def fetch_hustle(season):
    """Deflections, contested shots, screen assists, charges, loose balls (season totals)."""
    from nba_api.stats.endpoints import leaguehustlestatsplayer

    def call(t):
        return leaguehustlestatsplayer.LeagueHustleStatsPlayer(
            season=season, per_mode_time='Totals', season_type_all_star='Regular Season',
            timeout=t).get_data_frames()[0]

    return with_retries(call, 'hustle stats', attempts=3, base_wait=10, timeout=60)


def fetch_onoff(season, team_id):
    """One team's net rating with each player on and off the floor: (off_rows, on_rows)."""
    from nba_api.stats.endpoints import teamplayeronoffsummary

    def call(t):
        frames = teamplayeronoffsummary.TeamPlayerOnOffSummary(
            team_id=team_id, season=season, season_type_all_star='Regular Season',
            per_mode_detailed='Totals', timeout=t).get_data_frames()
        rows = [r for f in frames if 'COURT_STATUS' in f.columns for r in f.to_dict('records')]
        on = [r for r in rows if str(r.get('COURT_STATUS', '')).lower() == 'on']
        off = [r for r in rows if str(r.get('COURT_STATUS', '')).lower() == 'off']
        return off, on

    return with_retries(call, 'on/off %s' % team_id, attempts=2, base_wait=5, timeout=45, quiet=True)


def _f(v):
    try:
        v = float(v)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def add_extra_stats(players, season):
    """Pull the three optional data sets and attach them to each player.
    Anything NBA.com refuses is skipped; the site still builds without it."""
    by_id = {p['nba_id']: p for p in players}
    got = []
    # 0. last season's shooting (the track record hot/cold starts are judged against)
    try:
        df = fetch_player_stats(previous_season(season))
        for rec in df.to_dict('records'):
            p = by_id.get(num(rec.get('PLAYER_ID'), 0))
            gp = _f(rec.get('GP')) or 0
            if not p or not gp:
                continue
            p['ls3a'] = int(round((_f(rec.get('FG3A')) or 0) * gp))
            p['ls3m'] = int(round((_f(rec.get('FG3M')) or 0) * gp))
        got.append('last season')
        time.sleep(1.5)
    except Exception as e:  # noqa: BLE001
        print('       (Skipped last season: %s)' % e)
    # 1. defender distance shooting
    try:
        for key, ranges in DEF_RANGES.items():
            tot = {}
            for rng in ranges:
                df = fetch_pt_shots(season, rng)
                for rec in df.to_dict('records'):
                    pid = num(rec.get('PLAYER_ID'), 0)
                    if pid not in by_id:
                        continue
                    t = tot.setdefault(pid, [0.0, 0.0, 0.0, 0.0])   # FGM, FGA, 3PM, 3PA
                    t[0] += _f(rec.get('FGM')) or 0
                    t[1] += _f(rec.get('FGA')) or 0
                    t[2] += _f(rec.get('FG3M')) or 0
                    t[3] += _f(rec.get('FG3A')) or 0
                time.sleep(1.5)
            for pid, (m, a, m3, a3) in tot.items():
                if a > 0:
                    by_id[pid][key + 'FGA'] = int(a)
                    by_id[pid][key + 'EFG'] = round((m + 0.5 * m3) / a, 3)
                if a3 > 0:
                    by_id[pid][key + '3A'] = int(a3)
                    by_id[pid][key + '3M'] = int(m3)
        got.append('defender distance')
    except Exception as e:  # noqa: BLE001
        print('       (Skipped open/contested shots: %s)' % e)
    # 2. hustle
    try:
        df = fetch_hustle(season)
        for rec in df.to_dict('records'):
            p = by_id.get(num(rec.get('PLAYER_ID'), 0))
            mins = _f(rec.get('MIN'))
            if not p or not mins:
                continue
            per36 = lambda c: (_f(rec.get(c)) or 0) * 36.0 / mins
            p['HUS36'] = round(per36('DEFLECTIONS') + 0.5 * per36('CONTESTED_SHOTS') + 0.5 * per36('SCREEN_ASSISTS')
                               + 2 * per36('CHARGES_DRAWN') + per36('LOOSE_BALLS_RECOVERED'), 2)
            p['DEFL36'] = round(per36('DEFLECTIONS'), 1)
            p['CONT36'] = round(per36('CONTESTED_SHOTS'), 1)
        got.append('hustle')
        time.sleep(1.5)
    except Exception as e:  # noqa: BLE001
        print('       (Skipped hustle stats: %s)' % e)
    # 3. on/off, one request per team
    teams = sorted({p.get('tid') for p in players if p.get('tid')})
    ok = fails = 0
    best = {}
    for tid in teams:
        if fails >= 3 and not ok:   # NBA.com isn't answering these; don't spend an hour trying
            break
        try:
            off_df, on_df = fetch_onoff(season, tid)
            offm = {num(r.get('VS_PLAYER_ID'), 0): r for r in off_df}
            for r in on_df:
                pid = num(r.get('VS_PLAYER_ID'), 0)
                o = offm.get(pid)
                if pid not in by_id or not o:
                    continue
                on_min, off_min = _f(r.get('MIN')) or 0, _f(o.get('MIN')) or 0
                if pid in best and best[pid] >= on_min:   # traded: keep the team he played most for
                    continue
                best[pid] = on_min
                p = by_id[pid]
                p['ON_NET'], p['OFF_NET'] = _f(r.get('NET_RATING')), _f(o.get('NET_RATING'))
                p['ON_MIN'], p['OFF_MIN'] = round(on_min), round(off_min)
            ok += 1
        except Exception:  # noqa: BLE001
            fails += 1
        time.sleep(0.8)
    if ok:
        got.append('on/off for %d teams' % ok)
    else:
        print('       (Skipped on/off ratings: NBA.com did not answer)')
    return got


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


# ───────────────────────── Due to Break Out / Due to Cool Off ─────────────────────────
# Each player is checked against a few "profiles" that combine several stats, the way an
# analyst would read them together. Rules of thumb used (sources in automation/HOW_TO.txt):
#  - 3-point % needs ~300 attempts to trust, so a hot or cold start is judged against the
#    player's own track record (last season), or, with no track record, against what the league
#    makes on threes as open (or contested) as his.
#  - Efficiency doesn't naturally fall as usage rises, so a heavy-usage scorer is judged on
#    true shooting vs the league average.
#  - On/off takes hundreds of minutes to settle, so it is pulled toward zero (300-minute prior)
#    and only used next to other evidence.
AN_MIN_MPG = 15
AN_PICKS = 5
THREE_PRIOR = 300.0      # attempts of "track record" a new 3P% sample is blended with
ONOFF_PRIOR = 300.0      # minutes


def _z(values):
    """dict id -> value  =>  dict id -> z-score."""
    vals = [v for v in values.values() if v is not None]
    if len(vals) < 8:
        return {}
    mean = sum(vals) / len(vals)
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals)) or 1.0
    return {k: (v - mean) / sd for k, v in values.items() if v is not None}


def _pctl(values):
    """dict id -> value  =>  dict id -> percentile 0..100 within the dict."""
    order = sorted(values, key=lambda k: values[k])
    n = len(order)
    return {k: 100.0 * (i + 0.5) / n for i, k in enumerate(order)}


def shot_quality(shots, lg):
    """(actual eFG, expected eFG from where the shots came from, attempts)."""
    if not shots or not lg:
        return None
    made_pts = exp_pts = 0.0
    for x, y, m in shots:
        three = is_three(x, y)
        w = 1.5 if three else 1.0
        made_pts += w * m
        exp_pts += w * lg['3' if three else '2'][dist_bin(x, y)]
    n = len(shots)
    return made_pts / n, exp_pts / n, n


def _pc(v):
    return '%.1f' % (v * 100)


def build_analytics(players, shot_data, lg, games_so_far, game_logs=None):
    game_logs = game_logs or {}
    pool = [p for p in players if (p.get('MP_PG') or 0) >= AN_MIN_MPG and p.get('G', 0) >= 2]
    if len(pool) < 20:
        return None
    by = {p['nba_id']: p for p in pool}
    ids = list(by)

    # season totals (exact from game logs when we have them)
    T = {}
    for pid in ids:
        p, g = by[pid], game_logs.get(str(pid)) or game_logs.get(pid)
        if g:
            s = lambda i: sum(x[i] for x in g)
            T[pid] = {'fgm': s(10), 'fga': s(11), '3pm': s(12), '3pa': s(13), 'ftm': s(14), 'fta': s(15),
                      'pts': s(4), 'min': s(3), 'g': len(g)}
        else:
            G = p.get('G', 0)
            T[pid] = {'fga': (p.get('FGA_PG') or 0) * G, '3pa': (p.get('3PA_PG') or 0) * G,
                      '3pm': (p.get('3PA_PG') or 0) * G * (p.get('3P%') or 0), 'fta': (p.get('FTA_PG') or 0) * G,
                      'ftm': (p.get('FTA_PG') or 0) * G * (p.get('FT%') or 0), 'pts': (p.get('PTS_PG') or 0) * G,
                      'min': (p.get('MP_PG') or 0) * G, 'g': G}
    # league marks across the pool
    tot = lambda k: sum(T[i][k] for i in ids)
    lg_ts = tot('pts') / (2.0 * (tot('fga') + 0.44 * tot('fta'))) if tot('fga') else 0.57
    lg_3 = tot('3pm') / tot('3pa') if tot('3pa') else 0.36

    usg_p = _pctl({i: by[i].get('USG%') or 0 for i in ids})
    pts_p = _pctl({i: by[i].get('PTS_PG') or 0 for i in ids})
    pra = {i: (by[i].get('PTS_PG') or 0) + (by[i].get('TRB_PG') or 0) + (by[i].get('AST_PG') or 0) for i in ids}
    pra_p = _pctl(pra)
    # hustle percentile within position
    groups = {}
    for i in ids:
        if by[i].get('HUS36') is not None:
            groups.setdefault(str(by[i].get('Pos') or 'X').split('-')[0][:1].upper() or 'X', []).append(i)
    if any(len(v) < 12 for v in groups.values()):
        groups = {'X': [i for v in groups.values() for i in v]}
    hus_p, hus_grp = {}, {}
    for gname, mem in groups.items():
        hus_p.update(_pctl({i: by[i]['HUS36'] for i in mem}))
        for i in mem:
            hus_grp[i] = {'G': 'guards', 'F': 'forwards', 'C': 'centers'}.get(gname, 'rotation players')
    # league 3P% on open vs contested threes (for players without a track record)
    lg3r = {}
    for key in ('open', 'tight'):
        a3 = sum(by[i].get(key + '3A', 0) for i in ids)
        lg3r[key] = sum(by[i].get(key + '3M', 0) for i in ids) / a3 if a3 >= 100 else None
    # open/contested league eFG
    lgr = {}
    for key in ('open', 'tight'):
        a = sum(by[i].get(key + 'FGA', 0) for i in ids)
        lgr[key] = sum(by[i].get(key + 'EFG', 0) * by[i].get(key + 'FGA', 0) for i in ids) / a if a else None

    cards = {'b': [], 'c': []}
    for i in ids:
        p, t = by[i], T[i]
        name = p['Player'].split(' ')[-1]
        ts = t['pts'] / (2.0 * (t['fga'] + 0.44 * t['fta'])) if t['fga'] else None
        if ts is None or t['fga'] < 20:
            continue
        rts = ts - lg_ts
        usg = p.get('USG%') or 0
        ev = {}  # evidence chips: key -> [label, value, comparison, looks good right now? (None = neutral)]
        ev['ts'] = ['True shooting', _pc(ts) + '%', 'league ' + _pc(lg_ts) + '%', rts >= 0]
        ev['usg'] = ['Usage', _pc(usg) + '%', 'top %d%% of rotation' % max(1, 100 - int(usg_p[i])) if usg_p[i] >= 50
                     else 'bottom %d%%' % max(1, int(usg_p[i])), None]

        # three-point outlook: blend this season with his track record
        three = None
        if t['3pa'] >= 15 and (p.get('3PA_PG') or 0) >= 2:
            cur = t['3pm'] / t['3pa']
            oa, ta = p.get('open3A', 0), p.get('tight3A', 0)
            share = oa / float(oa + ta) if oa + ta >= 10 else None
            # what the league makes on threes as open (or as contested) as his
            look = ((oa * lg3r['open'] + ta * lg3r['tight']) / (oa + ta)) if share is not None and lg3r.get('open') and lg3r.get('tight') else None
            if (p.get('ls3a') or 0) >= 80:
                prior, src = p['ls3m'] / float(p['ls3a']), 'last'
            elif look is not None:
                prior, src = look, 'open'
            else:
                prior, src = lg_3, 'league'
            exp = (t['3pm'] + THREE_PRIOR * prior) / (t['3pa'] + THREE_PRIOR)
            ppg = (cur - exp) * (p.get('3PA_PG') or 0) * 3
            three = {'cur': cur, 'exp': exp, 'prior': prior, 'src': src, 'ppg': ppg, 'm': int(round(t['3pm'])), 'a': int(round(t['3pa'])),
                     'share': share, 'look': look}
            if share is not None and p.get('open3A', 0) >= 5 and lg3r.get('open'):
                o3 = p['open3M'] / float(p['open3A'])
                ev['open3'] = ['Open threes', _pc(o3) + '%', '%d of %d, league %s%%' % (p['open3M'], p['open3A'], _pc(lg3r['open'])), o3 >= lg3r['open']]
            ev['3p'] = ['3-point %', _pc(cur) + '%', 'expected ' + _pc(exp) + '%', cur >= exp]
        sq = shot_quality(shot_data.get(str(i)), lg)
        if sq and sq[2] >= 40:
            ev['sq'] = ['Shot-making', _pc(sq[0]) + '% eFG', 'shot spots predict ' + _pc(sq[1]) + '%', sq[0] >= sq[1]]
        for key, lab in (('open', 'Open looks'), ('tight', 'Contested shots')):
            if p.get(key + 'FGA', 0) >= 12 and lgr.get(key):
                ev[key] = [lab, _pc(p[key + 'EFG']) + '% eFG', 'league ' + _pc(lgr[key]) + '%', p[key + 'EFG'] >= lgr[key]]
        imp = None
        if p.get('ON_NET') is not None and p.get('OFF_NET') is not None and (p.get('OFF_MIN') or 0) >= 20:
            on_min = p.get('ON_MIN') or 0
            imp = (p['ON_NET'] - p['OFF_NET']) * on_min / (on_min + ONOFF_PRIOR)
            ev['onoff'] = ['On/off', '%+.1f' % (p['ON_NET'] - p['OFF_NET']), '%+.1f after adjusting for sample' % imp, imp >= 0]
        if i in hus_p:
            ev['hustle'] = ['Hustle', 'top %d%%' % max(1, 100 - int(hus_p[i])) if hus_p[i] >= 50 else 'bottom %d%%' % max(1, int(hus_p[i])),
                            'among ' + hus_grp[i], hus_p[i] >= 50]

        profiles = []   # (list, score, label, text, evidence keys, confidence)
        # COOL OFF 1: volume without efficiency
        if pts_p[i] >= 65 and usg_p[i] >= 60 and rts <= -0.025:
            sc = (-rts / 0.04) * (usg_p[i] / 100.0) * (t['fga'] / (t['fga'] + 60.0)) * 2.2
            fg = t['fgm'] / t['fga'] if t.get('fgm') is not None else p.get('FG%')
            txt = ('Scoring %.1f a game, but it takes %.1f shots and %.1f free throws to get there. '
                   'His %s%% true shooting is %.1f points below the league average while he uses %s%% of his team\'s plays%s, '
                   'so the points are coming from volume, not efficiency.') % (
                p.get('PTS_PG') or 0, p.get('FGA_PG') or 0, p.get('FTA_PG') or 0, _pc(ts), -rts * 100, _pc(usg),
                (' and shoots %s%% from the field' % _pc(fg)) if fg else '')
            profiles.append(('c', sc, 'Volume without efficiency', txt, ['ts', 'usg', 'sq', 'onoff'],
                             _conf(t['fga'], 150, 400, 'shots')))
        # COOL OFF 2 / BREAK OUT 1: three-point shooting vs track record
        if three and abs(three['ppg']) >= 0.8 and abs(three['cur'] - three['exp']) >= 0.05:
            hot = three['cur'] > three['exp']
            if three['src'] == 'last':
                base = 'His track record: %s%% on %d threes last season.' % (_pc(three['prior']), int(p['ls3a']))
                if three['share'] is not None and three['look'] is not None:
                    base += ' %d%% of his threes this season have been open; on looks like his, the league makes %s%%.' % (
                        round(three['share'] * 100), _pc(three['look']))
            elif three['src'] == 'open':
                base = ('He has no real track record yet, so the starting point is his shot mix: %d%% of his threes have been open, '
                        'and on looks like his the league makes %s%%.') % (round(three['share'] * 100), _pc(three['look']))
            else:
                base = 'He has no real track record yet, so the starting point is the league rate (%s%%).' % _pc(lg_3)
            txt = ('%s %s%% of his threes (%d of %d). %s Threes take about 300 attempts to trust, '
                   'so the best guess going forward is about %s%%: roughly %.1f %s points a game.') % (
                'Making' if hot else 'Making just', _pc(three['cur']), three['m'], three['a'], base, _pc(three['exp']),
                abs(three['ppg']), 'fewer' if hot else 'more')
            keys = ['3p', 'open3', 'open' if not hot else 'tight', 'sq', 'ts']
            profiles.append(('c' if hot else 'b', abs(three['ppg']) * 1.1, 'Hot from three' if hot else 'Cold from three', txt, keys,
                             _conf(three['a'], 100, 250, 'threes')))
        # BREAK OUT 2: efficient in a small role
        if rts >= 0.04 and usg_p[i] <= 45 and t['fga'] >= 25:
            sc = (rts / 0.04) * (1 - usg_p[i] / 100.0) * (t['fga'] / (t['fga'] + 60.0)) * 2.0
            txt = ('%s%% true shooting is %.1f points above the league average, but he only uses %s%% of his team\'s plays '
                   '(%.1f shots a game). Few rotation players score this efficiently; if his role grows, the points have room to follow.') % (
                _pc(ts), rts * 100, _pc(usg), p.get('FGA_PG') or 0)
            profiles.append(('b', sc, 'Efficient, under-used', txt, ['ts', 'usg', 'sq', 'onoff'], _conf(t['fga'], 150, 400, 'shots')))
        # BREAK OUT 3: impact the box score misses
        if imp is not None and imp >= 3 and i in hus_p and hus_p[i] >= 70 and pra_p[i] <= 60 and rts >= -0.04:
            sc = imp / 4.0 + (hus_p[i] - 50) / 40.0
            txt = ('His team is %+.1f per 100 possessions with him on the floor and %+.1f with him off (%d minutes), and he is in the '
                   'top %d%% of %s in hustle plays: %.1f deflections and %.1f contested shots per 36. His %.1f points, rebounds and assists miss most of that.') % (
                p['ON_NET'], p['OFF_NET'], p.get('ON_MIN') or 0, max(1, 100 - int(hus_p[i])), hus_grp[i],
                p.get('DEFL36') or 0, p.get('CONT36') or 0, pra[i])
            profiles.append(('b', sc, 'Impact beyond the box score', txt, ['onoff', 'hustle', 'ts'], _conf(p.get('ON_MIN') or 0, 300, 800, 'minutes')))
        # COOL OFF 3: big numbers, team gets outscored
        if imp is not None and imp <= -3 and pra_p[i] >= 75 and rts <= 0.01:
            sc = -imp / 4.0 + (pra_p[i] - 75) / 25.0
            txt = ('Putting up %.1f points, rebounds and assists, but his team is %+.1f per 100 with him on the floor and %+.1f with him off, '
                   'and his %s%% true shooting is %s the league average. Big counting stats on a losing floor tend to shrink once the minutes get tighter.') % (
                pra[i], p['ON_NET'], p['OFF_NET'], _pc(ts), 'below' if rts < 0 else 'only at')
            profiles.append(('c', sc, 'Big numbers, little impact', txt, ['onoff', 'ts', 'usg'], _conf(p.get('ON_MIN') or 0, 300, 800, 'minutes')))

        for side in ('b', 'c'):
            mine = sorted([x for x in profiles if x[0] == side], key=lambda x: -x[1])
            if not mine:
                continue
            best = mine[0]
            score = best[1] + 0.3 * sum(x[1] for x in mine[1:])
            keys = [k for k in best[4] if k in ev][:3]
            cards[side].append({
                'id': i, 'n': p['Player'], 't': p['Team'], 'pra': round(pra[i], 1), 's': round(score, 2),
                'lab': best[2],
                'txt': best[3], 'ev': [ev[k] for k in keys], 'conf': best[5],
                'also': [x[2] for x in mine[1:]],
            })

    # a player goes on only one list: the side where his case is stronger
    best_side = {}
    for side in ('b', 'c'):
        for c in cards[side]:
            if c['id'] not in best_side or c['s'] > best_side[c['id']][1]:
                best_side[c['id']] = (side, c['s'])
    out = {}
    for side in ('b', 'c'):
        cards[side] = [c for c in cards[side] if best_side[c['id']][0] == side]
        picked, per_label = [], {}
        for c in sorted(cards[side], key=lambda c: -c['s']):
            lab = c['lab']
            if per_label.get(lab, 0) >= 3:      # keep the list varied
                continue
            per_label[lab] = per_label.get(lab, 0) + 1
            picked.append(c)
            if len(picked) == AN_PICKS:
                break
        out[side] = picked
    return {'games': games_so_far, 'pool': len(pool), 'lgts': round(lg_ts * 100, 1), 'b': out['b'], 'c': out['c']}


def _conf(n, mid, high, unit):
    """How much to trust a profile, from its sample size."""
    lvl = 'High' if n >= high else 'Medium' if n >= mid else 'Low'
    return {'lvl': lvl, 'note': '%d %s so far' % (int(n), unit)}


def read_old_meta():
    """META from the current index.html (for pick history), or {}."""
    try:
        with open(os.path.join(HERE, 'index.html'), 'r', encoding='utf-8') as f:
            m = re.search(r'var META=(\{.*?\});\n', f.read(), re.S)
        return json.loads(m.group(1)) if m else {}
    except Exception:  # noqa: BLE001
        return {}


def add_to_history(history, analytics, today):
    """One entry per day: [date, [[id, pra]...] break out, [[id, pra]...] cool off]. Newest last."""
    day = today.strftime('%Y-%m-%d')
    history = [h for h in (history or []) if h and h[0] != day]
    if analytics and (analytics['b'] or analytics['c']):
        history.append([day, [[x['id'], x['pra']] for x in analytics['b']],
                        [[x['id'], x['pra']] for x in analytics['c']]])
    return history[-200:]


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


def restyle(rescore=False):
    """Rebuild index.html from its own embedded data with the latest template.html.
    rescore=True also recomputes the Break Out / Cool Off picks from that data."""
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
    if rescore:
        an = build_analytics(players, shots, meta.get('lg'), meta.get('gamesSoFar', 0), logs)
        meta['an'] = an
        meta['anHist'] = add_to_history(meta.get('anHist'), an, datetime.now().date())
        if an:
            print('Due to Break Out: %s' % ', '.join(x['n'] for x in an['b']))
            print('Due to Cool Off:  %s' % ', '.join(x['n'] for x in an['c']))
    with open(os.path.join(HERE, 'template.html'), 'r', encoding='utf-8') as f:
        template = f.read()
    write_atomic(path, render_page(template, players, shots, meta, meta.get('season', ''), logs))
    print('Restyled index.html (%d players, data from %s).' % (len(players), meta.get('updated')))
    print('NEXT: open GitHub Desktop, click "Commit to main", then "Push origin".')
    return 0


def datatest(season):
    """Quick check that NBA.com answers the three extra data requests."""
    for label, fn in (('Open shots', lambda: fetch_pt_shots(season, DEF_RANGES['open'][0])),
                      ('Hustle stats', lambda: fetch_hustle(season)),
                      ('On/off (Knicks)', lambda: fetch_onoff(season, 1610612752)[1])):
        try:
            r = fn()
            print('  OK    %-16s %d rows' % (label, len(r)))
        except Exception as e:  # noqa: BLE001
            print('  FAIL  %-16s %s' % (label, e))
    return 0


# ───────────────────────── Main ─────────────────────────

def main(argv):
    season = current_season()
    skip_shots = False
    if any(a.lower() == 'restyle' for a in argv):
        return restyle()
    if any(a.lower() == 'rescore' for a in argv):
        return restyle(rescore=True)
    if any(a.lower() == 'datatest' for a in argv):
        return datatest(season)
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

    # Extra data + Due to Break Out / Due to Cool Off (optional: never blocks the update)
    analytics, history = None, []
    try:
        print('       Pulling open/contested shots, hustle stats and on/off ratings...')
        got = add_extra_stats(eligible, season)
        if got:
            print('       Got: %s.' % ', '.join(got))
        analytics = build_analytics(eligible, shot_data, lg, games_so_far, game_logs)
        old_meta = read_old_meta()
        history = old_meta.get('anHist', []) if old_meta.get('season') == season else []
        history = add_to_history(history, analytics, today)
        if analytics:
            print('       Due to Break Out: %s' % ', '.join(x['n'] for x in analytics['b']))
            print('       Due to Cool Off:  %s' % ', '.join(x['n'] for x in analytics['c']))
    except Exception as e:  # noqa: BLE001
        print('       (Break Out / Cool Off lists skipped: %s)' % e)

    now = datetime.now()
    meta = {
        'season': season,
        'updated': '%s %d, %d' % (now.strftime('%b'), now.day, now.year),
        'gamesSoFar': games_so_far,
        'minGP': min_needed,
        'pct': int(MIN_GP_FRACTION * 100),
        'lg': lg,
        'movers': movers,
        'an': analytics,
        'anHist': history,
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
