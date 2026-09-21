"""
NFL DATA -- nflverse data layer for the NFL bot (Phase 2).

Pulls the official nflverse-data release CSVs straight from GitHub (no
package, no scraping -- these files are published for programs, CC-BY):

  injuries_{season}.csv          official weekly injury report incl. practice status
  snap_counts_{season}.csv       per-game snap counts + snap share (PFR)
  stats_player_week_{season}.csv per-game player box lines + target share / WOPR
  roster_weekly_{season}.csv     weekly roster + status (ACT/RES/INA/CUT/...)
  depth_charts_{season}.csv      depth-chart snapshots (we keep only the latest)
  games.csv                      schedule + results + closing lines

Files are cached on disk (NFL_DATA_DIR, /data/nflverse when the volume is
mounted) and refreshed every NFL_DATA_TTL_MIN (default 360 = 6h). nflverse
updates nightly in season, so 6h is plenty and a redeploy never re-downloads
a fresh file.

Everything here is read-only plumbing. No modelling, no blending -- the
commands show real numbers and label the sample.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime

import pandas as pd
import requests

log = logging.getLogger("nfl_data")

RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"
SEASON = int(os.getenv("NFL_SEASON", "0") or 0) or (
    datetime.now().year if datetime.now().month >= 8 else datetime.now().year - 1)
PREV = SEASON - 1
DATA_DIR = os.getenv("NFL_DATA_DIR") or (
    "/data/nflverse" if os.path.isdir("/data") else os.path.join(os.getcwd(), "nflverse_cache"))
TTL = max(5, int(os.getenv("NFL_DATA_TTL_MIN", "360") or 360)) * 60
TIMEOUT = 120

FILES = {
    "injuries":   f"injuries/injuries_{SEASON}.csv",
    "snaps":      f"snap_counts/snap_counts_{SEASON}.csv",
    "stats":      f"stats_player/stats_player_week_{SEASON}.csv",
    "stats_prev": f"stats_player/stats_player_week_{PREV}.csv",
    "roster":     f"weekly_rosters/roster_weekly_{SEASON}.csv",
    "depth":      f"depth_charts/depth_charts_{SEASON}.csv",
    "games":      "schedules/games.csv",
}

_mem: dict[str, tuple[float, pd.DataFrame]] = {}


# ---------------------------------------------------------------- teams
TEAMS = {
    "ARI": "Cardinals", "ATL": "Falcons", "BAL": "Ravens", "BUF": "Bills",
    "CAR": "Panthers", "CHI": "Bears", "CIN": "Bengals", "CLE": "Browns",
    "DAL": "Cowboys", "DEN": "Broncos", "DET": "Lions", "GB": "Packers",
    "HOU": "Texans", "IND": "Colts", "JAX": "Jaguars", "KC": "Chiefs",
    "LA": "Rams", "LAC": "Chargers", "LV": "Raiders", "MIA": "Dolphins",
    "MIN": "Vikings", "NE": "Patriots", "NO": "Saints", "NYG": "Giants",
    "NYJ": "Jets", "PHI": "Eagles", "PIT": "Steelers", "SEA": "Seahawks",
    "SF": "49ers", "TB": "Buccaneers", "TEN": "Titans", "WAS": "Commanders",
}
_ALIASES = {"LAR": "LA", "RAMS": "LA", "JAC": "JAX", "WSH": "WAS", "OAK": "LV",
            "SD": "LAC", "STL": "LA", "NINERS": "SF", "BUCS": "TB", "PATS": "NE",
            "JAGS": "JAX", "CARDS": "ARI", "SKINS": "WAS", "COMMIES": "WAS"}


def resolve_team(text: str) -> str | None:
    """'kc' / 'chiefs' / 'Kansas City' / 'LAR' -> 'KC' / 'LA'."""
    if not text:
        return None
    t = text.strip().upper().replace(".", "")
    if t in TEAMS:
        return t
    if t in _ALIASES:
        return _ALIASES[t]
    for abbr, nick in TEAMS.items():
        if t == nick.upper() or t in nick.upper() or nick.upper() in t:
            return abbr
    cities = {"KANSAS": "KC", "GREEN BAY": "GB", "NEW ENGLAND": "NE", "NEW ORLEANS": "NO",
              "TAMPA": "TB", "LAS VEGAS": "LV", "SAN FRANCISCO": "SF", "NEW YORK GIANTS": "NYG",
              "NEW YORK JETS": "NYJ", "LOS ANGELES RAMS": "LA", "LOS ANGELES CHARGERS": "LAC",
              "WASHINGTON": "WAS", "PHILLY": "PHI", "PHILADELPHIA": "PHI", "JACKSONVILLE": "JAX",
              "ARIZONA": "ARI", "ATLANTA": "ATL", "BALTIMORE": "BAL", "BUFFALO": "BUF",
              "CAROLINA": "CAR", "CHICAGO": "CHI", "CINCINNATI": "CIN", "CLEVELAND": "CLE",
              "DALLAS": "DAL", "DENVER": "DEN", "DETROIT": "DET", "HOUSTON": "HOU",
              "INDIANAPOLIS": "IND", "INDY": "IND", "MIAMI": "MIA", "MINNESOTA": "MIN",
              "PITTSBURGH": "PIT", "SEATTLE": "SEA", "TENNESSEE": "TEN"}
    for city, abbr in cities.items():
        if city in t:
            return abbr
    return None


# ---------------------------------------------------------------- fetch/cache

def _path(key: str) -> str:
    return os.path.join(DATA_DIR, FILES[key].replace("/", "__"))


def _download(key: str) -> bytes | None:
    url = f"{RELEASES}/{FILES[key]}"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            log.warning("nflverse %s -> HTTP %d", FILES[key], r.status_code)
            return None
        return r.content
    except Exception:
        log.exception("nflverse download failed: %s", url)
        return None


def load(key: str, force: bool = False) -> pd.DataFrame:
    """DataFrame for one dataset. Memory cache -> disk cache (TTL) -> download.
    Never raises; an unfetchable file comes back as an empty frame so the
    command can say 'no data' instead of crashing."""
    now = time.time()
    if not force and key in _mem and now - _mem[key][0] < TTL:
        return _mem[key][1]
    os.makedirs(DATA_DIR, exist_ok=True)
    p = _path(key)
    fresh = os.path.exists(p) and (now - os.path.getmtime(p)) < TTL and not force
    if not fresh:
        blob = _download(key)
        if blob:
            with open(p, "wb") as f:
                f.write(blob)
            log.info("nflverse: refreshed %s (%d KB)", FILES[key], len(blob) // 1024)
        elif not os.path.exists(p):
            _mem[key] = (now, pd.DataFrame())
            return _mem[key][1]
        else:
            log.warning("nflverse: using stale cache for %s", FILES[key])
    try:
        df = pd.read_csv(p, low_memory=False)
        if key == "depth":
            # 190+ snapshots a season -- keep only the latest one per team.
            latest = df.groupby("team")["dt"].transform("max")
            df = df[df["dt"] == latest].copy()
        if key == "games":
            df = df[df["season"] == SEASON].copy()
    except Exception:
        log.exception("nflverse: parse failed for %s", p)
        df = pd.DataFrame()
    _mem[key] = (now, df)
    return df


def file_age_min(key: str) -> int | None:
    p = _path(key)
    return int((time.time() - os.path.getmtime(p)) // 60) if os.path.exists(p) else None


def refresh_all() -> None:
    for k in FILES:
        load(k)


# ---------------------------------------------------------------- players

def latest_week(df: pd.DataFrame) -> int:
    return int(df["week"].max()) if len(df) and "week" in df else 0


def resolve_player(text: str, stats: pd.DataFrame | None = None,
                   roster: pd.DataFrame | None = None) -> list[tuple[str, str, str]]:
    """Substring/last-name match -> [(player_id, display_name, team)].
    Prefers this season's stat lines (they carry the current team), falls
    back to the weekly roster for guys who haven't recorded a stat yet."""
    q = (text or "").strip().lower()
    if not q:
        return []
    stats = load("stats") if stats is None else stats
    roster = load("roster") if roster is None else roster
    out: dict[str, tuple[str, str, str]] = {}
    if len(stats):
        s = stats[stats["player_display_name"].str.lower().str.contains(q, na=False, regex=False)]
        s = s.sort_values("week")
        for _, r in s.iterrows():
            out[r["player_id"]] = (r["player_id"], r["player_display_name"], r["team"])
    if not out and len(roster):
        wk = latest_week(roster)
        r = roster[(roster["week"] == wk) & roster["full_name"].str.lower().str.contains(q, na=False, regex=False)]
        for _, row in r.iterrows():
            out[row["gsis_id"]] = (row["gsis_id"], row["full_name"], row["team"])
    return list(out.values())
