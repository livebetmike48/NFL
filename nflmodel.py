"""
NFL MODEL -- point-in-time yardage projections for the three alert markets
(passing / rushing / receiving yards). Phase 3 of the NFL bot.

Every number is a real, traceable rate. Structure per market:

    projection = VOLUME x EFFICIENCY x OPPONENT
    VOLUME     = blend(this-season per-game attempts|carries|targets,
                       last-season per-game)          weights n/(n+K_VOL)
    EFFICIENCY = blend(this-season yds per attempt|carry|target,
                       last-season)                   weights n/(n+K_EFF)
    OPPONENT   = 1 + SHRINK x (defense allowed-per-game to this position
                              / league average - 1)   blended the same way

Blends use the standard n/(n+K) shrinkage: a player with n games this
season gets weight n/(n+K) on his 2026 rate and the rest on his 2025 rate
(or the position average if he has no prior season). K_VOL is small because
roles change fast; K_EFF is larger because efficiency is noisy.

Probabilities at a line are NON-PARAMETRIC: P(actual > line) is read off
the empirical distribution of (actual / projection) ratios from the
previous season's point-in-time projections, per position group + market.
Yards are right-skewed and zero-heavy (a WR can catch nothing); a normal
curve gets the tails wrong, and the ratio table is the honest fix. The
table is fit ONCE from a completed season and stored -- it is never fit on
the games it is grading.

Point-in-time discipline: project(season, week) uses ONLY games from weeks
< week of that season plus the prior season. That is what the backtest
runs and what the live bot runs; same code path.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

log = logging.getLogger("nflmodel")

K_VOL = 3.0      # games of shrinkage for volume
MIN_SNAP_PCT = 0.20   # a game under this offense snap share isn't a "game played"
PARTIAL_FRAC = 0.5    # ...nor is one under half the player's own peak share (left early)
RECENCY = 0.6         # per-game decay for volume: last game 1.0, prior 0.6, 0.36...
K_EFF = 8.0      # games of shrinkage for efficiency
K_DEF = 6.0      # games of shrinkage for defense-allowed rates
SHRINK_DEF = 0.5 # how much of a defense's deviation from league avg we apply
MIN_VOL = {"pass": 10.0, "rush": 3.0, "rec": 2.0, "recs": 2.0}   # per-game volume floor to project
# How much to trust LAST season: scaled by games played then (a rookie or an
# injury year is thin evidence) and halved on a team change (new scheme,
# new role). This is the Kaleb Johnson fix.
PRIOR_FULL_GAMES = 8.0
NEW_TEAM_PRIOR = 0.5

MARKETS = {
    # market -> (volume col, outcome col, position groups it applies to)
    "pass": ("attempts", "passing_yards", {"QB"}),
    "rush": ("carries", "rushing_yards", {"RB", "QB", "WR"}),
    "rec":  ("targets", "receiving_yards", {"WR", "TE", "RB"}),
    "recs": ("targets", "receptions", {"WR", "TE", "RB"}),      # efficiency = catch rate
}
ODDS_MARKET = {"pass": "player_pass_yds", "rush": "player_rush_yds",
               "rec": "player_reception_yds", "recs": "player_receptions"}
ODDS_ALT = {"pass": "player_pass_yds_alternate", "rush": "player_rush_yds_alternate",
            "rec": "player_reception_yds_alternate", "recs": "player_receptions_alternate"}
ODDS_TD = "player_anytime_td"
MK_LABEL = {"pass": "Pass Yds", "rush": "Rush Yds", "rec": "Rec Yds", "recs": "Receptions", "td": "Anytime TD"}
POS_GROUP = {"QB": "QB", "RB": "RB", "FB": "RB", "HB": "RB", "WR": "WR", "TE": "TE"}


def _reg(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["season_type"] == "REG"] if "season_type" in df else df


def _pos(df: pd.DataFrame) -> pd.Series:
    return df["position"].map(POS_GROUP).fillna("")


# ------------------------------------------------------------------ rates

def _drop_partial_games(df: pd.DataFrame, snaps: pd.DataFrame | None) -> pd.DataFrame:
    """Remove partial player-games: under MIN_SNAP_PCT offense snaps, or under
    half the player's own peak share this season (left early / limited).
    Those weeks drag a season average down and make the market's "over"
    look like our "under"."""
    if snaps is None or snaps.empty or df.empty:
        return df
    sn = snaps[["season", "week", "player", "team", "offense_pct"]].copy()
    sn["k"] = sn["player"].str.lower().str.replace(r"[^a-z ]", "", regex=True)
    key = df["player_display_name"].str.lower().str.replace(r"[^a-z ]", "", regex=True)
    m = df.copy().assign(k=key).merge(sn, on=["season", "week", "team", "k"], how="left")
    peak = m.groupby("player_id")["offense_pct"].transform("max")
    floor = (peak * PARTIAL_FRAC).clip(lower=MIN_SNAP_PCT)
    keep = m["offense_pct"].isna() | (m["offense_pct"] >= floor)
    return df[keep.values]


def _player_rates(df: pd.DataFrame, recency: bool = False) -> pd.DataFrame:
    """Per-player per-game volume + efficiency over the rows given.
    recency=True weights volume toward the most recent weeks (role changes
    show up); efficiency is always a plain per-unit rate.
    -> index player_id; cols: name, team, pos, games, <mkt>_vol, <mkt>_eff"""
    if df.empty:
        return pd.DataFrame()
    d = df.copy()
    d["pos"] = _pos(d)
    d = d[d["pos"] != ""]
    if recency:
        last = d.groupby("player_id")["week"].transform("max")
        d["w"] = RECENCY ** (last - d["week"])
    else:
        d["w"] = 1.0
    g = d.groupby("player_id")
    out = pd.DataFrame({
        "name": g["player_display_name"].last(),
        "team": g["team"].last(),
        "pos": g["pos"].last(),
        "games": g["week"].nunique(),
    })
    wsum = g["w"].sum()
    for mk, (vol, yds, _) in MARKETS.items():
        v = g[vol].sum()
        y = g[yds].sum()
        out[f"{mk}_vol"] = (d[vol] * d["w"]).groupby(d["player_id"]).sum() / wsum
        out[f"{mk}_eff"] = (y / v).where(v > 0)
        out[f"{mk}_n"] = g[vol].apply(lambda s: int((s > 0).sum()))
    tds = d["rushing_tds"].fillna(0) + d["receiving_tds"].fillna(0)
    out["td_pg"] = (tds * d["w"]).groupby(d["player_id"]).sum() / wsum
    return out


def _pos_avg_eff(rates: pd.DataFrame) -> dict[tuple[str, str], float]:
    """Position-group average efficiency, volume-weighted -> {(pos, mkt): eff}."""
    out = {}
    for mk in MARKETS:
        for pos, grp in rates.groupby("pos"):
            v = grp[f"{mk}_vol"] * grp["games"]
            y = grp[f"{mk}_eff"] * v
            tot = v.sum()
            if tot > 0:
                out[(pos, mk)] = float(y.sum() / tot)
    return out


def _defense_rates(df: pd.DataFrame) -> pd.DataFrame:
    """Yards allowed per game by defense x position group x market.
    -> index (defense_team, pos); cols <mkt>_allowed, games"""
    if df.empty:
        return pd.DataFrame()
    d = df.copy()
    d["pos"] = _pos(d)
    d = d[d["pos"] != ""]
    games = d.groupby("opponent_team")["game_id"].nunique()
    rows = {}
    for mk, (_, yds, poss) in MARKETS.items():
        sub = d[d["pos"].isin(poss)]
        s = sub.groupby(["opponent_team", "pos"])[yds].sum()
        rows[f"{mk}_allowed"] = s
    tds = d["rushing_tds"].fillna(0) + d["receiving_tds"].fillna(0)
    rows["td_allowed"] = tds.groupby([d["opponent_team"], d["pos"]]).sum()
    out = pd.DataFrame(rows).fillna(0.0)
    out["games"] = out.index.get_level_values(0).map(games).values
    for mk in list(MARKETS) + ["td"]:
        out[f"{mk}_allowed"] = out[f"{mk}_allowed"] / out["games"]
    return out


def _blend(cur, n_cur, prior, k, prior_strength: float = 1.0) -> float:
    """n/(n+K) shrinkage toward the prior; either side may be missing.
    prior_strength in (0, 1] scales K down when the prior is thin or stale."""
    cur_ok = cur is not None and not (isinstance(cur, float) and math.isnan(cur))
    pri_ok = prior is not None and not (isinstance(prior, float) and math.isnan(prior))
    if cur_ok and pri_ok:
        w = n_cur / (n_cur + k * max(0.05, prior_strength))
        return w * float(cur) + (1 - w) * float(prior)
    if cur_ok:
        return float(cur)
    if pri_ok:
        return float(prior)
    return float("nan")


# ------------------------------------------------------------------ projections

@dataclass
class Projection:
    player_id: str
    name: str
    team: str
    pos: str
    market: str          # pass | rush | rec
    opponent: str
    volume: float
    efficiency: float
    opp_factor: float
    proj: float
    games_cur: int       # this-season games behind the volume blend
    opp_rank: int = 0    # 1 = opponent allows the MOST to this position (this season)

    def why(self) -> str:
        if self.market == "td":
            return (f"{self.volume:.2f} TD/g x {self.opp_factor:.2f} vs {self.opponent} "
                    f"= {self.proj:.2f} exp TD")
        vol_lbl = {"pass": "att", "rush": "car", "rec": "tgt", "recs": "tgt"}[self.market]
        if self.market == "recs":
            eff = f"{self.efficiency:.0%} catch"
        else:
            eff = f"{self.efficiency:.2f} y/{vol_lbl}"
        return (f"{self.volume:.1f} {vol_lbl} x {eff} x {self.opp_factor:.2f} "
                f"vs {self.opponent} = {self.proj:.1f}")

    def matchup(self) -> str:
        lbl = MK_LABEL.get(self.market, self.market)
        if not self.opp_rank:
            return ""
        what = "TDs" if self.market == "td" else lbl.lower()
        return f"{self.opponent} allows {_ordinal(self.opp_rank)}-most {what} to {self.pos}s"


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def project(cur: pd.DataFrame, prior: pd.DataFrame, week: int,
            schedule: pd.DataFrame, season: int,
            snaps: pd.DataFrame | None = None) -> list[Projection]:
    """Point-in-time projections for every eligible player in `week`.
    cur   = this season's weekly player stats (ALL weeks; filtered here)
    prior = last season's weekly player stats
    schedule = games.csv rows for `season`"""
    cur_pt = _reg(cur)
    cur_pt = _drop_partial_games(cur_pt[cur_pt["week"] < week], snaps)
    prior_reg = _reg(prior)
    r_cur = _player_rates(cur_pt, recency=True)
    r_pri = _player_rates(prior_reg)
    pos_eff = _pos_avg_eff(r_pri if not r_pri.empty else r_cur)
    d_cur = _defense_rates(cur_pt)
    d_pri = _defense_rates(prior_reg)

    # league average allowed per position (for the opponent factor)
    def lg_avg(d: pd.DataFrame, pos: str, mk: str) -> float:
        if d.empty:
            return float("nan")
        sub = d[d.index.get_level_values(1) == pos]
        return float(sub[f"{mk}_allowed"].mean()) if len(sub) else float("nan")

    # who plays whom this week (REG only)
    wk = schedule[(schedule["season"] == season) & (schedule["week"] == week)
                  & (schedule["game_type"] == "REG")]
    opp = {}
    for _, g in wk.iterrows():
        opp[g["home_team"]] = g["away_team"]
        opp[g["away_team"]] = g["home_team"]

    # the player universe: anyone with a stat line this season, or last season
    # if we're at week 1 (then last season's team is the best we have)
    universe = r_cur if not r_cur.empty else r_pri
    out: list[Projection] = []
    for pid, row in universe.iterrows():
        team = row["team"]
        if team not in opp:
            continue
        pos = row["pos"]
        o = opp[team]
        c = r_cur.loc[pid] if pid in r_cur.index else None
        p = r_pri.loc[pid] if pid in r_pri.index else None
        n_games = int(c["games"]) if c is not None else 0
        strength = 1.0
        if p is not None:
            strength = min(1.0, float(p["games"]) / PRIOR_FULL_GAMES)
            if c is not None and p["team"] != c["team"]:
                strength *= NEW_TEAM_PRIOR
        for mk, (_, _, poss) in MARKETS.items():
            if pos not in poss:
                continue
            vol = _blend(c[f"{mk}_vol"] if c is not None else None, n_games,
                         p[f"{mk}_vol"] if p is not None else None, K_VOL, strength)
            if math.isnan(vol) or vol < MIN_VOL[mk]:
                continue
            n_eff = int(c[f"{mk}_n"]) if c is not None else 0
            eff = _blend(c[f"{mk}_eff"] if c is not None else None, n_eff,
                         p[f"{mk}_eff"] if p is not None else pos_eff.get((pos, mk)), K_EFF,
                         strength if p is not None else 1.0)
            if math.isnan(eff):
                eff = pos_eff.get((pos, mk), float("nan"))
            if math.isnan(eff):
                continue
            # opponent factor: blend the defense's allowed-per-game ratio to league
            def ratio(d: pd.DataFrame) -> tuple[float, int]:
                if d.empty or (o, pos) not in d.index:
                    return float("nan"), 0
                lg = lg_avg(d, pos, mk)
                if not lg or math.isnan(lg) or lg <= 0:
                    return float("nan"), 0
                return float(d.loc[(o, pos), f"{mk}_allowed"] / lg), int(d.loc[(o, pos), "games"])
            rc, nc = ratio(d_cur)
            rp, _ = ratio(d_pri)
            r = _blend(rc, nc, rp, K_DEF)
            factor = 1.0 + SHRINK_DEF * (r - 1.0) if not math.isnan(r) else 1.0
            factor = min(max(factor, 0.6), 1.5)
            out.append(Projection(pid, row["name"], team, pos, mk, o,
                                  vol, eff, factor, vol * eff * factor, n_games,
                                  _rank(d_cur if not d_cur.empty else d_pri, o, pos, mk)))
        # anytime TD: expected TDs per game (rush + rec), opponent-adjusted; P = 1 - e^-x
        if pos in ("RB", "WR", "TE", "QB"):
            td = _blend(c["td_pg"] if c is not None else None, n_games,
                        p["td_pg"] if p is not None else None, K_EFF, strength)
            if not math.isnan(td) and td >= 0.05:
                rc, nc = ratio_generic(d_cur, o, pos, "td")
                rp, _ = ratio_generic(d_pri, o, pos, "td")
                r = _blend(rc, nc, rp, K_DEF)
                factor = 1.0 + SHRINK_DEF * (r - 1.0) if not math.isnan(r) else 1.0
                factor = min(max(factor, 0.6), 1.5)
                out.append(Projection(pid, row["name"], team, pos, "td", o, td, 1.0, factor,
                                      td * factor, n_games,
                                      _rank(d_cur if not d_cur.empty else d_pri, o, pos, "td")))
    return out


def ratio_generic(d: pd.DataFrame, opp: str, pos: str, mk: str) -> tuple[float, int]:
    if d.empty or (opp, pos) not in d.index:
        return float("nan"), 0
    sub = d[d.index.get_level_values(1) == pos]
    lg = float(sub[f"{mk}_allowed"].mean()) if len(sub) else float("nan")
    if not lg or math.isnan(lg) or lg <= 0:
        return float("nan"), 0
    return float(d.loc[(opp, pos), f"{mk}_allowed"] / lg), int(d.loc[(opp, pos), "games"])


def _rank(d: pd.DataFrame, opp: str, pos: str, mk: str) -> int:
    """1 = allows the most per game to this position, among defenses on file."""
    if d.empty or (opp, pos) not in d.index:
        return 0
    sub = d[d.index.get_level_values(1) == pos][f"{mk}_allowed"].sort_values(ascending=False)
    return int(list(sub.index.get_level_values(0)).index(opp)) + 1


def p_anytime_td(exp_td: float) -> float:
    return 1.0 - math.exp(-max(exp_td, 0.0))


# ------------------------------------------------------------------ probability

class RatioTable:
    """Empirical actual/projection ratios per (pos, market), fit on one
    completed season's point-in-time projections. P(over line) is the share
    of ratios above line/proj. Stored as sorted arrays; never refit on the
    season being graded."""

    def __init__(self):
        self.tables: dict[tuple[str, str], np.ndarray] = {}

    def fit(self, cur: pd.DataFrame, prior: pd.DataFrame, schedule: pd.DataFrame,
            season: int, weeks: range = range(3, 19), progress=None,
            snaps: pd.DataFrame | None = None) -> "RatioTable":
        actual = _reg(cur).set_index(["player_id", "week"])
        buckets: dict[tuple[str, str], list[float]] = {}
        for wk in weeks:
            for pr in project(cur, prior, wk, schedule, season, snaps):
                if pr.market == "td":
                    continue          # TD is Poisson, not a ratio
                key = (pr.player_id, wk)
                if key not in actual.index:
                    continue          # didn't play -> prop would be void
                ycol = MARKETS[pr.market][1]
                a = float(actual.loc[key, ycol]) if not isinstance(actual.loc[key], pd.DataFrame) \
                    else float(actual.loc[key].iloc[0][ycol])
                buckets.setdefault((pr.pos, pr.market), []).append(a / pr.proj if pr.proj > 0 else 0.0)
            if progress:
                progress(wk)
        self.tables = {k: np.sort(np.array(v)) for k, v in buckets.items() if len(v) >= 50}
        return self

    def p_over(self, pos: str, market: str, proj: float, line: float) -> float | None:
        t = self.tables.get((pos, market))
        if t is None or proj <= 0:
            return None
        r = line / proj
        # strict "over" a .5 line = actual > line
        return float(1.0 - np.searchsorted(t, r, side="right") / len(t))

    def to_dict(self) -> dict:
        return {f"{p}|{m}": v.tolist() for (p, m), v in self.tables.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "RatioTable":
        rt = cls()
        for k, v in d.items():
            p, m = k.split("|")
            rt.tables[(p, m)] = np.sort(np.array(v, dtype=float))
        return rt

    def summary(self) -> str:
        lines = []
        for (p, m), t in sorted(self.tables.items()):
            q = np.quantile(t, [0.1, 0.25, 0.5, 0.75, 0.9])
            lines.append(f"{p:<3}{m:<5} n={len(t):>5} p10={q[0]:.2f} p25={q[1]:.2f} "
                         f"p50={q[2]:.2f} p75={q[3]:.2f} p90={q[4]:.2f}")
        return "\n".join(lines)


def implied(american: float | None) -> float | None:
    if american is None:
        return None
    a = float(american)
    return 100.0 / (a + 100.0) if a > 0 else -a / (-a + 100.0)


def calibration_table(rows: list[tuple[float, int]], bins=(0, .4, .5, .6, .7, .8, 1.01)) -> str:
    """rows = [(model_prob, hit 0/1)] -> text table of predicted vs actual."""
    if not rows:
        return "(no rows)"
    arr = np.array(rows)
    out = ["bucket     n    pred   actual"]
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (arr[:, 0] >= lo) & (arr[:, 0] < hi)
        if m.sum() == 0:
            continue
        out.append(f"{lo:.2f}-{hi:.2f} {int(m.sum()):>5}   {arr[m, 0].mean():.3f}   {arr[m, 1].mean():.3f}")
    brier = float(((arr[:, 0] - arr[:, 1]) ** 2).mean())
    out.append(f"Brier {brier:.4f}  (n={len(arr)})")
    return "\n".join(out)
