#!/usr/bin/env python3
"""
NBA Savant Data Updater
=======================
Pulls fresh NBA data and generates index.html for your site.

SETUP (one time only):
  pip install nba_api pandas

USAGE:
  python update_nba_savant.py

Then drag the 'nba-savant' folder to Netlify to deploy.
"""

import json, time, os, sys, unicodedata

try:
    import pandas as pd
    from nba_api.stats.endpoints import leaguedashplayerstats
    from nba_api.stats.static import players as static_players
except ImportError:
    print("ERROR: Missing required packages.")
    print("Run this command first:")
    print("  pip install nba_api pandas")
    sys.exit(1)

print()
print("=" * 50)
print("  NBA SAVANT - Data Updater")
print("=" * 50)
print()

# ── STEP 1: Pull basic per-game stats ──
print("[1/3] Pulling basic per-game stats from NBA.com...")
try:
    basic = leaguedashplayerstats.LeagueDashPlayerStats(
        season='2025-26',
        per_mode_detailed='PerGame',
        season_type_all_star='Regular Season'
    )
    df_basic = basic.get_data_frames()[0]
    print(f"       ✓ {len(df_basic)} players")
except Exception as e:
    print(f"  ERROR: {e}")
    print("  NBA.com may be temporarily unavailable. Try again in a minute.")
    sys.exit(1)

time.sleep(1.5)

# ── STEP 2: Pull advanced stats ──
print("[2/3] Pulling advanced stats from NBA.com...")
try:
    adv = leaguedashplayerstats.LeagueDashPlayerStats(
        season='2025-26',
        measure_type_detailed_defense='Advanced',
        per_mode_detailed='PerGame',
        season_type_all_star='Regular Season'
    )
    df_adv = adv.get_data_frames()[0]
    print(f"       ✓ {len(df_adv)} players")
except Exception as e:
    print(f"  ERROR: {e}")
    sys.exit(1)

# ── STEP 3: Merge and build ──
print("[3/3] Building player database...")

# Merge basic + advanced on PLAYER_ID
df = df_basic.merge(
    df_adv[['PLAYER_ID','OFF_RATING','DEF_RATING','NET_RATING',
            'AST_PCT','AST_TO','AST_RATIO','OREB_PCT','DREB_PCT',
            'REB_PCT','EFG_PCT','TS_PCT','USG_PCT','PACE','PIE']],
    on='PLAYER_ID', how='left'
)

def to_ascii(s):
    return ''.join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))

def safe(val, decimals=1):
    if val is None or pd.isna(val): return None
    v = float(val)
    return str(round(v, decimals))

def safe_pct(val):
    """For values like 0.567 -> keep as 0.567 (JS will handle display)"""
    if val is None or pd.isna(val): return None
    return str(round(float(val), 3))

players = []
for _, r in df.iterrows():
    p = {
        'Player': to_ascii(str(r['PLAYER_NAME'])),
        'nba_id': int(r['PLAYER_ID']),
        'Team': str(r['TEAM_ABBREVIATION']),
        'Age': safe(r.get('AGE'), 0),
        'G': str(int(r['GP'])),
    }
    # Per-game stats (already per-game from API)
    for col, key in [('MIN','MP_PG'),('PTS','PTS_PG'),('REB','TRB_PG'),
                      ('AST','AST_PG'),('STL','STL_PG'),('BLK','BLK_PG'),
                      ('TOV','TOV_PG'),('FGM','FG_PG'),('FGA','FGA_PG'),
                      ('FG3M','3P_PG'),('FTM','FT_PG'),
                      ('OREB','ORB_PG'),('DREB','DRB_PG')]:
        v = safe(r.get(col))
        if v: p[key] = v
    
    # Shooting %
    for col, key in [('FG_PCT','FG%'),('FG3_PCT','3P%'),('FT_PCT','FT%')]:
        v = safe_pct(r.get(col))
        if v: p[key] = v
    
    # Advanced
    for col, key in [('EFG_PCT','eFG%'),('TS_PCT','TS%'),('USG_PCT','USG%'),
                      ('AST_PCT','AST%'),('OREB_PCT','ORB%'),('DREB_PCT','DRB%'),
                      ('REB_PCT','TRB%'),('OFF_RATING','ORTG'),('DEF_RATING','DRTG'),
                      ('NET_RATING','NET_RTG'),('PIE','PIE'),('PACE','PACE'),
                      ('AST_TO','AST_TO')]:
        v = safe(r.get(col))
        if v: p[key] = v
    
    # Plus/minus
    v = safe(r.get('PLUS_MINUS'))
    if v: p['PM'] = v
    
    players.append(p)

# Remove any player with 0 games
players = [p for p in players if int(p.get('G', '0')) > 0]
print(f"       ✓ {len(players)} active players processed")

# ── STEP 4: Pull shot chart data for qualifying players ──
print("[4/4] Pulling shot chart data...")
from nba_api.stats.endpoints import shotchartdetail

shot_data = {}
qual_players = [p for p in players if int(p.get('G', '0')) >= 20]
total = len(qual_players)
for idx, p in enumerate(qual_players):
    pid = p['nba_id']
    try:
        sc = shotchartdetail.ShotChartDetail(
            team_id=0,
            player_id=pid,
            context_measure_simple='FGA',
            season_nullable='2025-26',
            season_type_all_star='Regular Season'
        )
        df_shots = sc.get_data_frames()[0]
        if len(df_shots) > 0:
            # Compress: just store [x, y, made] for each shot
            shots = []
            for _, s in df_shots.iterrows():
                shots.append([int(s['LOC_X']), int(s['LOC_Y']), 1 if s['SHOT_MADE_FLAG'] == 1 else 0])
            shot_data[str(pid)] = shots
        if (idx + 1) % 25 == 0:
            print(f"       ... {idx+1}/{total} players")
        time.sleep(0.4)  # Be nice to NBA.com
    except Exception as e:
        pass  # Skip players with no shot data

print(f"       ✓ Shot charts for {len(shot_data)} players")

shots_json = json.dumps(shot_data, separators=(',', ':'))
data_json = json.dumps(players, ensure_ascii=True)

HTML_TEMPLATE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'template.html'), 'r').read()

html_output = HTML_TEMPLATE.replace('__DATA_PLACEHOLDER__', data_json).replace('__SHOTS_PLACEHOLDER__', shots_json)
script_dir = os.path.dirname(os.path.abspath(__file__))
output_dir = os.path.join(script_dir, 'nba-savant')
os.makedirs(output_dir, exist_ok=True)
output_path = os.path.join(output_dir, 'index.html')
with open(output_path, 'w', encoding='utf-8') as f:
    f.write(html_output)
print()
print("=" * 50)
print(f"  SUCCESS! Generated: nba-savant/index.html")
print(f"  Players: {len(players)}")
print(f"  File size: {os.path.getsize(output_path) / 1024:.0f} KB")
print("=" * 50)
print()
print("  Next: drag the 'nba-savant' folder to Netlify")
print()
