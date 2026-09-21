"""
NFL STATS -- the NFL bot's stats half (Phase 2). All numbers come straight
from nflverse (see nfl_data.py); every embed labels its sample so a Week-2
number never reads like a season number.

Commands (all command-only, nothing auto-posts):
  /snaps <team> [pos]        snap share by player, week by week, with trend
  /usage <player>            snap% / targets / target share / carries / WOPR by week
  /playerstats <player>      game log + season totals for the guy's position group
  /defense [team] [pos]      yards + TDs ALLOWED per game to QB/RB/WR/TE, league
                             rank (1 = allows the MOST), this season next to last
  /injuries <team>           the official report: status + practice participation
  /depth <team>              offensive depth chart (latest snapshot)
  /nflroster <team>          roster status changes vs last week (the derived
                             transactions log -- no free NFL transactions API exists)

Blending last season into this season is a MODEL decision (Phase 3), not a
display decision: these commands show both seasons side by side and rank
on the current one, with the game count in the footer.
"""
from __future__ import annotations

import logging

import discord
import pandas as pd
from discord import app_commands

import nfl_data as D

log = logging.getLogger("nflstats")

OFF_POS = ["QB", "RB", "WR", "TE"]
POS_CHOICES = [app_commands.Choice(name=p, value=p) for p in OFF_POS]
COLOR = 0x1f8b4c


def _pct(x) -> str:
    try:
        return f"{float(x) * 100:.0f}%"
    except Exception:
        return "—"


def _n(x, d=0) -> str:
    try:
        v = float(x)
        return f"{v:.{d}f}" if d else f"{int(round(v))}"
    except Exception:
        return "—"


def _code(lines: list[str]) -> str:
    body = "\n".join(lines)
    return "```\n" + body[:1900] + "\n```"


def _no_data(what: str) -> str:
    return f"No {what} on file yet — nflverse hasn't published it, or the download failed (check the log)."


# ---------------------------------------------------------------- /snaps

def snaps_table(team: str, pos: str | None = None) -> tuple[str, str] | None:
    sc = D.load("snaps")
    if not len(sc):
        return None
    t = sc[(sc["team"] == team) & (sc["offense_snaps"] > 0)]
    if pos:
        t = t[t["position"] == pos]
    else:
        t = t[t["position"].isin(OFF_POS)]
    if not len(t):
        return None
    weeks = sorted(t["week"].unique())
    piv = t.pivot_table(index=["player", "position"], columns="week", values="offense_pct", aggfunc="max")
    piv["avg"] = piv[weeks].mean(axis=1)
    piv = piv.reset_index()
    piv["_p"] = pd.Categorical(piv["position"], OFF_POS)
    piv = piv.sort_values(["_p", "avg"], ascending=[True, False])
    hdr = f"{'Player':<22}{'Pos':<4}" + "".join(f"{'W' + str(w):>6}" for w in weeks) + f"{'Avg':>6}  Trend"
    lines = [hdr]
    for _, row in piv.iterrows():
        name, p = row["player"], row["position"]
        cells = "".join(f"{_pct(row.get(w)) if pd.notna(row.get(w)) else '—':>6}" for w in weeks)
        trend = ""
        if len(weeks) >= 2 and pd.notna(row.get(weeks[-1])) and pd.notna(row.get(weeks[-2])):
            d = (row[weeks[-1]] - row[weeks[-2]]) * 100
            trend = f"{'▲' if d >= 5 else '▼' if d <= -5 else '·'} {d:+.0f}"
        lines.append(f"{name[:21]:<22}{p:<4}{cells}{_pct(row['avg']):>6}  {trend}")
    title = f"{D.TEAMS.get(team, team)} snap share" + (f" — {pos}" if pos else "")
    return title, _code(lines[:40])


# ---------------------------------------------------------------- /usage

def usage_table(player_id: str) -> tuple[str, str, str] | None:
    st = D.load("stats")
    sc = D.load("snaps")
    if not len(st):
        return None
    p = st[st["player_id"] == player_id].sort_values("week")
    if not len(p):
        return None
    name, team, pos = p.iloc[-1]["player_display_name"], p.iloc[-1]["team"], p.iloc[-1]["position"]
    snap_by_wk = {}
    if len(sc):
        s = sc[(sc["player"] == name) & (sc["team"] == team)]
        snap_by_wk = dict(zip(s["week"], s["offense_pct"]))
    lines = [f"{'Wk':<4}{'Opp':<5}{'Snap%':>6}{'Tgt':>5}{'Tgt%':>6}{'AY%':>6}{'WOPR':>6}{'Car':>5}{'Rec':>5}{'Yds':>6}{'TD':>4}"]
    for _, r in p.iterrows():
        yds = (r.get("receiving_yards", 0) or 0) + (r.get("rushing_yards", 0) or 0)
        td = (r.get("receiving_tds", 0) or 0) + (r.get("rushing_tds", 0) or 0)
        if pos == "QB":
            yds, td = r.get("passing_yards", 0), r.get("passing_tds", 0)
        lines.append(f"{'W' + str(int(r['week'])):<4}{r['opponent_team']:<5}"
                     f"{_pct(snap_by_wk.get(r['week'])) if r['week'] in snap_by_wk else '—':>6}"
                     f"{_n(r.get('targets')):>5}{_pct(r.get('target_share')):>6}{_pct(r.get('air_yards_share')):>6}"
                     f"{_n(r.get('wopr'), 2):>6}{_n(r.get('carries')):>5}{_n(r.get('receptions')):>5}"
                     f"{_n(yds):>6}{_n(td):>4}")
    if len(p) >= 2:
        avg = p[["targets", "target_share", "air_yards_share", "wopr", "carries", "receptions"]].mean()
        snaps = [snap_by_wk[w] for w in p["week"] if w in snap_by_wk]
        lines.append(f"{'avg':<9}{_pct(sum(snaps) / len(snaps)) if snaps else '—':>6}{_n(avg['targets'], 1):>5}"
                     f"{_pct(avg['target_share']):>6}{_pct(avg['air_yards_share']):>6}{_n(avg['wopr'], 2):>6}"
                     f"{_n(avg['carries'], 1):>5}{_n(avg['receptions'], 1):>5}")
    return f"{name} ({team} {pos}) — usage by week", _code(lines), f"{len(p)} game(s) • Tgt% = share of team targets • AY% = share of team air yards • WOPR = 1.5×Tgt% + 0.7×AY%"


# ---------------------------------------------------------------- /playerstats

def playerstats_table(player_id: str) -> tuple[str, str, str] | None:
    st = D.load("stats")
    if not len(st):
        return None
    p = st[st["player_id"] == player_id].sort_values("week")
    if not len(p):
        return None
    name, team, pos = p.iloc[-1]["player_display_name"], p.iloc[-1]["team"], p.iloc[-1]["position"]
    if pos == "QB":
        cols = [("Cmp", "completions", 0), ("Att", "attempts", 0), ("PaYd", "passing_yards", 0), ("PaTD", "passing_tds", 0),
                ("INT", "passing_interceptions", 0), ("Sk", "sacks_suffered", 0), ("Car", "carries", 0), ("RuYd", "rushing_yards", 0), ("RuTD", "rushing_tds", 0)]
    elif pos == "RB":
        cols = [("Car", "carries", 0), ("RuYd", "rushing_yards", 0), ("RuTD", "rushing_tds", 0), ("Tgt", "targets", 0),
                ("Rec", "receptions", 0), ("ReYd", "receiving_yards", 0), ("ReTD", "receiving_tds", 0), ("Fum", "fumbles_lost_total", 0)]
    else:
        cols = [("Tgt", "targets", 0), ("Rec", "receptions", 0), ("ReYd", "receiving_yards", 0), ("ReTD", "receiving_tds", 0),
                ("AirYd", "receiving_air_yards", 0), ("YAC", "receiving_yards_after_catch", 0), ("20+", "receiving_20", 0), ("Car", "carries", 0), ("RuYd", "rushing_yards", 0)]
    hdr = f"{'Wk':<4}{'Opp':<5}" + "".join(f"{c[0]:>6}" for c in cols)
    lines = [hdr]
    for _, r in p.iterrows():
        lines.append(f"{'W' + str(int(r['week'])):<4}{r['opponent_team']:<5}" + "".join(f"{_n(r.get(c[1]), c[2]):>6}" for c in cols))
    tot = p[[c[1] for c in cols]].sum()
    lines.append(f"{'tot':<9}" + "".join(f"{_n(tot[c[1]]):>6}" for c in cols))
    if len(p) >= 2:
        avg = p[[c[1] for c in cols]].mean()
        lines.append(f"{'avg':<9}" + "".join(f"{_n(avg[c[1]], 1):>6}" for c in cols))
    return f"{name} ({team} {pos}) — {D.SEASON} game log", _code(lines), f"{len(p)} game(s) • nflverse"


# ---------------------------------------------------------------- /defense

_DEF_METRICS = {
    "QB": [("PaYd", "passing_yards"), ("PaTD", "passing_tds"), ("INT", "passing_interceptions"), ("Sk", "sacks_suffered")],
    "RB": [("RuYd", "rushing_yards"), ("RuTD", "rushing_tds"), ("Rec", "receptions"), ("ReYd", "receiving_yards")],
    "WR": [("Rec", "receptions"), ("ReYd", "receiving_yards"), ("ReTD", "receiving_tds"), ("Tgt", "targets")],
    "TE": [("Rec", "receptions"), ("ReYd", "receiving_yards"), ("ReTD", "receiving_tds"), ("Tgt", "targets")],
}


def defense_allowed(stats: pd.DataFrame, pos: str) -> pd.DataFrame:
    """Per-game totals ALLOWED by each defense to one position group.
    Games = distinct game_ids in which that defense appeared."""
    if not len(stats):
        return pd.DataFrame()
    metrics = [m for _, m in _DEF_METRICS[pos]]
    s = stats[stats["position"] == pos]
    games = stats.groupby("opponent_team")["game_id"].nunique().rename("games")
    tot = s.groupby("opponent_team")[metrics].sum()
    df = tot.join(games, how="right").fillna(0)
    for m in metrics:
        df[m] = df[m] / df["games"].replace(0, pd.NA)
    return df


def defense_table(pos: str, team: str | None = None) -> tuple[str, str, str] | None:
    cur = defense_allowed(D.load("stats"), pos)
    prev = defense_allowed(D.load("stats_prev"), pos)
    if not len(cur):
        return None
    lead = _DEF_METRICS[pos][0][1]
    cur = cur.sort_values(lead, ascending=False)
    cur["rank"] = range(1, len(cur) + 1)
    if len(prev):
        prev = prev.sort_values(lead, ascending=False)
        prev["rank"] = range(1, len(prev) + 1)
    labels = [lbl for lbl, _ in _DEF_METRICS[pos]]
    metrics = [m for _, m in _DEF_METRICS[pos]]
    if team:
        if team not in cur.index:
            return None
        r = cur.loc[team]
        lines = [f"{'':<8}" + "".join(f"{l:>7}" for l in labels) + f"{'Rank':>6}{'G':>4}",
                 f"{D.SEASON:<8}" + "".join(f"{_n(r[m], 1):>7}" for m in metrics) + f"{int(r['rank']):>6}{int(r['games']):>4}"]
        if len(prev) and team in prev.index:
            q = prev.loc[team]
            lines.append(f"{D.PREV:<8}" + "".join(f"{_n(q[m], 1):>7}" for m in metrics) + f"{int(q['rank']):>6}{int(q['games']):>4}")
        return (f"{D.TEAMS.get(team, team)} defense vs {pos} — allowed per game", _code(lines),
                f"rank 1 of 32 = allows the MOST • {D.SEASON} is {int(r['games'])} game(s); read it next to {D.PREV}")
    prev_hdr = f"{D.PREV} rk"
    lines = [f"{'#':<3}{'Team':<5}" + "".join(f"{l:>7}" for l in labels) + f"{prev_hdr:>9}"]
    for t, r in cur.iterrows():
        pr = int(prev.loc[t]["rank"]) if len(prev) and t in prev.index else "—"
        lines.append(f"{int(r['rank']):<3}{t:<5}" + "".join(f"{_n(r[m], 1):>7}" for m in metrics) + f"{pr:>9}")
    g = int(cur["games"].min()), int(cur["games"].max())
    return (f"Defense vs {pos} — allowed per game, {D.SEASON}", _code(lines),
            f"1 = allows the most • {g[0]}–{g[1]} games played • last column = where they ranked in {D.PREV}")


# ---------------------------------------------------------------- /injuries

def injuries_table(team: str) -> tuple[str, str, str] | None:
    inj = D.load("injuries")
    if not len(inj):
        return None
    wk = D.latest_week(inj)
    t = inj[(inj["team"] == team) & (inj["week"] == wk)]
    if not len(t):
        return (f"{D.TEAMS.get(team, team)} injury report — Week {wk}", "No one listed.", "official NFL report via nflverse")
    order = {"Out": 0, "Doubtful": 1, "Questionable": 2}
    t = t.assign(_o=t["report_status"].map(order).fillna(3)).sort_values(["_o", "position", "full_name"])
    prac = {"Did Not Participate In Practice": "DNP", "Limited Participation in Practice": "LIM",
            "Full Participation in Practice": "FULL"}
    lines = [f"{'Player':<22}{'Pos':<4}{'Status':<13}{'Prac':<6}Injury"]
    for _, r in t.iterrows():
        status = r["report_status"] if pd.notna(r["report_status"]) else "—"
        p = prac.get(r["practice_status"], (r["practice_status"] or "—")[:5] if pd.notna(r["practice_status"]) else "—")
        injury = r["report_primary_injury"] if pd.notna(r["report_primary_injury"]) else (
            r["practice_primary_injury"] if pd.notna(r["practice_primary_injury"]) else "")
        if pd.notna(r.get("report_secondary_injury")) and r.get("report_secondary_injury"):
            injury = f"{injury}/{r['report_secondary_injury']}"
        lines.append(f"{r['full_name'][:21]:<22}{r['position']:<4}{status:<13}{p:<6}{injury}")
    return (f"{D.TEAMS.get(team, team)} injury report — Week {wk}", _code(lines[:45]),
            "official NFL report as of the last practice day • NOT the inactives list (drops 90 min before kickoff)")


# ---------------------------------------------------------------- /depth

def depth_table(team: str) -> tuple[str, str, str] | None:
    dc = D.load("depth")
    if not len(dc):
        return None
    t = dc[dc["team"] == team]
    if not len(t):
        return None
    off = t[t["pos_abb"].isin(["QB", "RB", "FB", "WR", "TE", "LWR", "RWR", "SWR", "HB"])]
    if not len(off):
        off = t[t["pos_grp"].str.contains("Off", na=False)]
    lines = []
    for slot, grp in off.groupby("pos_abb", sort=False):
        grp = grp.sort_values("pos_rank")
        names = " › ".join(f"{r['player_name']}" for _, r in grp.iterrows())
        lines.append(f"{slot:<5}{names}")
    order = {"QB": 0, "HB": 1, "RB": 1, "FB": 2, "WR": 3, "LWR": 3, "RWR": 4, "SWR": 5, "TE": 6}
    lines.sort(key=lambda s: order.get(s.split()[0], 9))
    when = str(t["dt"].max())[:10]
    return f"{D.TEAMS.get(team, team)} offensive depth chart", _code(lines), f"snapshot {when} • nflverse depth_charts"


# ---------------------------------------------------------------- /nflroster

def roster_changes(team: str) -> tuple[str, str, str] | None:
    ro = D.load("roster")
    if not len(ro):
        return None
    wk = D.latest_week(ro)
    cur = ro[(ro["team"] == team) & (ro["week"] == wk)].set_index("gsis_id")
    prev = ro[(ro["team"] == team) & (ro["week"] == wk - 1)].set_index("gsis_id")
    if not len(cur):
        return None
    label = {"ACT": "active", "RES": "IR/reserve", "INA": "inactive", "CUT": "released", "DEV": "practice squad",
             "RET": "retired", "EXE": "exempt", "PUP": "PUP", "SUS": "suspended"}
    lines = []
    for pid, r in cur.iterrows():
        was = prev.loc[pid]["status"] if pid in prev.index else None
        now = r["status"]
        if was is None:
            lines.append(f"➕ {r['full_name']} ({r['position']}) — joined ({label.get(now, now)})")
        elif was != now:
            lines.append(f"🔁 {r['full_name']} ({r['position']}) — {label.get(was, was)} → {label.get(now, now)}")
    for pid, r in prev.iterrows():
        if pid not in cur.index and r["status"] not in ("CUT", "RET"):
            lines.append(f"➖ {r['full_name']} ({r['position']}) — off the roster (was {label.get(r['status'], r['status'])})")
    if not lines:
        lines = ["No status changes vs last week."]
    return (f"{D.TEAMS.get(team, team)} roster changes — Week {wk - 1} → Week {wk}", "\n".join(lines[:40]),
            "derived from weekly roster status (nflverse); weekly, not same-day")


# ---------------------------------------------------------------- discord glue

def _embed(title: str, body: str, footer: str | None = None) -> discord.Embed:
    e = discord.Embed(title=title[:256], description=body[:4000], color=COLOR)
    if footer:
        e.set_footer(text=footer[:2048])
    return e


def _guarded(fn):
    import functools

    @functools.wraps(fn)
    async def run(interaction, *a, **k):
        try:
            await fn(interaction, *a, **k)
        except Exception as e:
            log.exception("nfl stats command %s failed", fn.__name__)
            msg = f"⚠️ Command failed — {type(e).__name__}: {e}"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg[:1900])
                else:
                    await interaction.response.send_message(msg[:1900])
            except Exception:
                pass
    return run


async def _pick_player(interaction, text: str):
    """Resolve a name; sends the ambiguity/none message itself and returns None."""
    import asyncio
    hits = await asyncio.to_thread(D.resolve_player, text)
    if not hits:
        await interaction.followup.send(f"No NFL player matching “{text}” this season.")
        return None
    if len(hits) > 1:
        exact = [h for h in hits if h[1].lower() == text.strip().lower()]
        if len(exact) == 1:
            return exact[0]
        opts = ", ".join(f"{n} ({t})" for _, n, t in hits[:8])
        await interaction.followup.send(f"Which one? {opts}")
        return None
    return hits[0]


DESC = {
    "snaps": "NFL snap share by player, week by week, with trend",
    "snaps.team": "Team (abbr or nickname)",
    "snaps.position": "Optional: QB / RB / WR / TE",
    "usage": "NFL player usage by week: snap%, targets, target share, air yards, WOPR",
    "usage.player": "Player name (partial fine)",
    "playerstats": "NFL player game log + season totals",
    "playerstats.player": "Player name (partial fine)",
    "defense": "Yards/TDs allowed per game to a position — league table or one team",
    "defense.position": "Position group (default RB)",
    "defense.team": "Optional: one team",
    "injuries": "NFL injury report for a team: status + practice participation",
    "injuries.team": "Team (abbr or nickname)",
    "depth": "NFL offensive depth chart (latest snapshot)",
    "depth.team": "Team (abbr or nickname)",
    "nflroster": "Roster status changes vs last week (IR, activations, cuts, signings)",
    "nflroster.team": "Team (abbr or nickname)",
}


def setup(bot):
    import asyncio
    tree = bot.tree

    async def _team_cmd(interaction, team_text: str, builder, what: str, *args):
        await interaction.response.defer()
        team = D.resolve_team(team_text)
        if not team:
            await interaction.followup.send(f"Don't know a team called “{team_text}”. Try an abbreviation like KC or a nickname like Chiefs.")
            return
        res = await asyncio.to_thread(builder, team, *args)
        if not res:
            await interaction.followup.send(_no_data(f"{what} for {D.TEAMS.get(team, team)}"))
            return
        await interaction.followup.send(embed=_embed(*res))

    @tree.command(name="snaps", description=DESC["snaps"])
    @app_commands.describe(team=DESC["snaps.team"], position=DESC["snaps.position"])
    @app_commands.choices(position=POS_CHOICES)
    @_guarded
    async def snaps_cmd(interaction: discord.Interaction, team: str,
                        position: app_commands.Choice[str] | None = None):
        def _build(t, p):
            r = snaps_table(t, p)
            return (r[0], r[1], "offense_pct from nflverse snap_counts") if r else None
        await _team_cmd(interaction, team, _build, "snap counts", position.value if position else None)

    @tree.command(name="usage", description=DESC["usage"])
    @app_commands.describe(player=DESC["usage.player"])
    @_guarded
    async def usage_cmd(interaction: discord.Interaction, player: str):
        await interaction.response.defer()
        hit = await _pick_player(interaction, player)
        if not hit:
            return
        res = await asyncio.to_thread(usage_table, hit[0])
        await interaction.followup.send(embed=_embed(*res) if res else _no_data(f"usage for {hit[1]}"))

    @tree.command(name="playerstats", description=DESC["playerstats"])
    @app_commands.describe(player=DESC["playerstats.player"])
    @_guarded
    async def playerstats_cmd(interaction: discord.Interaction, player: str):
        await interaction.response.defer()
        hit = await _pick_player(interaction, player)
        if not hit:
            return
        res = await asyncio.to_thread(playerstats_table, hit[0])
        await interaction.followup.send(embed=_embed(*res) if res else _no_data(f"stats for {hit[1]}"))

    @tree.command(name="defense", description=DESC["defense"])
    @app_commands.describe(position=DESC["defense.position"], team=DESC["defense.team"])
    @app_commands.choices(position=POS_CHOICES)
    @_guarded
    async def defense_cmd(interaction: discord.Interaction,
                          position: app_commands.Choice[str] | None = None,
                          team: str | None = None):
        await interaction.response.defer()
        pos = position.value if position else "RB"
        t = None
        if team:
            t = D.resolve_team(team)
            if not t:
                await interaction.followup.send(f"Don't know a team called “{team}”.")
                return
        res = await asyncio.to_thread(defense_table, pos, t)
        await interaction.followup.send(embed=_embed(*res) if res else _no_data("defensive splits"))

    @tree.command(name="injuries", description=DESC["injuries"])
    @app_commands.describe(team=DESC["injuries.team"])
    @_guarded
    async def injuries_cmd(interaction: discord.Interaction, team: str):
        await _team_cmd(interaction, team, injuries_table, "injury report")

    @tree.command(name="depth", description=DESC["depth"])
    @app_commands.describe(team=DESC["depth.team"])
    @_guarded
    async def depth_cmd(interaction: discord.Interaction, team: str):
        await _team_cmd(interaction, team, depth_table, "depth chart")

    @tree.command(name="nflroster", description=DESC["nflroster"])
    @app_commands.describe(team=DESC["nflroster.team"])
    @_guarded
    async def nflroster_cmd(interaction: discord.Interaction, team: str):
        await _team_cmd(interaction, team, roster_changes, "roster data")

    log.info("nflstats: registered /snaps /usage /playerstats /defense /injuries /depth /nflroster")


def start(bot):
    """Warm the cache on boot so the first command isn't a 30-second download."""
    import asyncio
    if getattr(bot, "_nflstats_task", None) is None:
        async def warm():
            await bot.wait_until_ready()
            await asyncio.to_thread(D.refresh_all)
            log.info("nflstats: nflverse cache warm (%s) — season %d", D.DATA_DIR, D.SEASON)
        bot._nflstats_task = bot.loop.create_task(warm())
