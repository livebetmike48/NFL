"""
NFL PARLAY -- Phase 3 of the NFL bot. Bot Cooks architecture, NFL data.

    model (nflmodel)  x  lines (nflprops tracker DB, zero extra credits)
        -> P(over)/P(under) per player-market from the empirical ratio table
        -> quality bars (real thresholds, not a score)
        -> one leg per TEAM (no same-game stacking), sorted by edge
        -> leg count is the OUTPUT; "no parlay this week" when nothing clears

Posts once a week (Wednesday NFL_PARLAY_POST_ET, default 12:00) through a
webhook displaying as "NFL Parlay" -- only after /nflbacktest has been run
and NFL_PARLAY_CHANNEL_ID is set. Every leg is stored and graded against
nflverse box scores the following Tuesday (/nflrecord = the public
forward log).

Quality bars (env-tunable, defaults):
  NFL_BAR_P      0.60   model probability on the chosen side
  NFL_BAR_EDGE   0.05   model P minus the implied P of the best price
  NFL_MIN_PRICE  -150   never lay more than this
  NFL_MAX_LEGS   6      cap (min is 2; fewer = no parlay)
  NFL_MIN_GAMES  2      this-season games behind the volume blend

Injury gate: Out / Doubtful on the latest official report are excluded;
Questionable stays in but is flagged on the leg.

Commands:
  /nflparlay [post]      build this week's slate now (dry run unless post)
  /nflrecord             forward log: every posted leg, graded, units
  /nflbacktest [season]  grade the model on a past season with REAL closing
                         lines from the Odds API historical endpoint
                         (~8.5K credits for a season). Posts receipts:
                         calibration table, Brier vs market, units at the
                         bar, parlay record. Progress edits as it runs.

Ratio table: fit at boot from (SEASON-1) projections with (SEASON-2) as
prior -- i.e. 2024 with a 2023 prior for a 2026 bot -- and cached on the
volume. It is never fit on the season it grades.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
import pandas as pd
from discord import app_commands

import nfl_data
import nfl_odds
import nflmodel as M
import nflprops

log = logging.getLogger("nflparlay")
ET = ZoneInfo("America/New_York")

SEASON = nfl_data.SEASON
BAR_P = float(os.getenv("NFL_BAR_P", "0.60"))
BAR_EDGE = float(os.getenv("NFL_BAR_EDGE", "0.05"))
MIN_PRICE = int(os.getenv("NFL_MIN_PRICE", "-150"))
MAX_LEGS = max(2, int(os.getenv("NFL_MAX_LEGS", "6")))
MIN_GAMES = int(os.getenv("NFL_MIN_GAMES", "2"))
# floor on the posted line -- keeps 1.5-yard novelty props out of the slate
MIN_LINE = {"pass": 150.0, "rush": 20.0, "rec": 15.0}
POST_ET = os.getenv("NFL_PARLAY_POST_ET", "12:00")
CHANNEL_ID = int(os.getenv("NFL_PARLAY_CHANNEL_ID", "0") or 0)
WEBHOOK_NAME = "LBM NFL Parlay"
DISPLAY_NAME = os.getenv("NFL_PARLAY_NAME", "NFL Parlay")
DB = nflprops.DB                      # same volume DB as the tracker
RATIO_PATH = os.path.join(nfl_data.DATA_DIR, f"ratio_table_{SEASON - 1}.json")

_ratio: M.RatioTable | None = None


# ------------------------------------------------------------------ storage

def _conn():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS nfl_parlay_legs (
        season INTEGER, week INTEGER, posted_ts INTEGER, player_id TEXT,
        player TEXT, team TEXT, market TEXT, side TEXT, line REAL,
        price INTEGER, book TEXT, proj REAL, p REAL, edge REAL,
        actual REAL, result TEXT,
        PRIMARY KEY (season, week, player_id, market))""")
    return c


# ------------------------------------------------------------------ names

_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?")


def norm_name(s: str) -> str:
    s = (s or "").lower().replace(".", "").replace("'", "").replace("-", " ")
    s = _SUFFIX.sub("", s)
    return " ".join(s.split())


def _team_from_full(full: str) -> str | None:
    """'Kansas City Chiefs' -> 'KC' via the nickname table."""
    f = (full or "").lower()
    for ab, nick in nfl_data.TEAMS.items():
        if nick.lower() in f:
            return ab
    return None


# ------------------------------------------------------------------ ratio table

def _fit_ratio_table(progress=None) -> M.RatioTable:
    fit_season, prior_season = SEASON - 1, SEASON - 2
    cur = nfl_data.load_season("stats", fit_season)
    pri = nfl_data.load_season("stats", prior_season)
    games = nfl_data.games_all()
    rt = M.RatioTable().fit(cur, pri, games, fit_season, progress=progress)
    try:
        with open(RATIO_PATH, "w") as f:
            json.dump(rt.to_dict(), f)
    except Exception:
        log.exception("ratio table cache write failed")
    return rt


def ratio_table() -> M.RatioTable:
    global _ratio
    if _ratio is not None:
        return _ratio
    if os.path.exists(RATIO_PATH):
        try:
            with open(RATIO_PATH) as f:
                _ratio = M.RatioTable.from_dict(json.load(f))
            log.info("nflparlay: ratio table loaded from %s", RATIO_PATH)
            return _ratio
        except Exception:
            log.exception("ratio table cache read failed — refitting")
    t0 = time.time()
    _ratio = _fit_ratio_table()
    log.info("nflparlay: ratio table fit on %d (prior %d) in %.0fs\n%s",
             SEASON - 1, SEASON - 2, time.time() - t0, _ratio.summary())
    return _ratio


# ------------------------------------------------------------------ lines

def current_week() -> int:
    """The week whose lines are up = first REG week with an unplayed game."""
    g = nfl_data.load("games")
    g = g[g["game_type"] == "REG"]
    open_ = g[g["result"].isna()]
    return int(open_["week"].min()) if not open_.empty else int(g["week"].max())


def tracker_lines(now: int | None = None) -> dict[tuple[str, str], dict]:
    """Best available price per side from the tracker DB, pregame only.
    -> {(norm_name, mkt): {"over": (line, price, book), "under": (...),
                           "teams": {home, away}}}"""
    out: dict[tuple[str, str], dict] = {}
    evmap = nflprops._events_map()
    for mk, odds_mk in M.ODDS_MARKET.items():
        for mkt, player, book, _open, now_row, ev in nflprops.open_now(None, odds_mk, pregame_only=True, now=now):
            ts, line, over, under = now_row
            if line is None:
                continue
            key = (norm_name(player), mk)
            e = out.setdefault(key, {"over": None, "under": None, "player": player,
                                     "teams": set(), "event_id": ev})
            evrow = evmap.get(ev)
            if evrow:
                e["teams"] = {_team_from_full(evrow[1]), _team_from_full(evrow[2])}
            for side, price in (("over", over), ("under", under)):
                if price is None:
                    continue
                # best price = highest American odds for the bettor
                if e[side] is None or price > e[side][1]:
                    e[side] = (float(line), int(price), book)
    return out


# ------------------------------------------------------------------ legs

def injury_gate(week: int) -> dict[str, str]:
    """norm_name -> 'Out' | 'Doubtful' | 'Questionable' from the latest
    official report for `week` (or the latest week on file)."""
    inj = nfl_data.load("injuries")
    if inj.empty:
        return {}
    inj = inj[inj["season"] == SEASON]
    wk = inj[inj["week"] == week]
    if wk.empty:
        wk = inj[inj["week"] == inj["week"].max()]
    out = {}
    for _, r in wk.iterrows():
        st = str(r.get("report_status") or "")
        if st in ("Out", "Doubtful", "Questionable"):
            out[norm_name(r["full_name"])] = st
    return out


def evaluate(projs: list[M.Projection], lines: dict, rt: M.RatioTable,
             injuries: dict[str, str] | None = None) -> list[dict]:
    """Every projection that has a line -> one candidate row with the model
    P on both sides, the chosen side, and the edge at the best price.
    Bars are NOT applied here (the backtest needs every row for calibration)."""
    injuries = injuries or {}
    rows = []
    for pr in projs:
        key = (norm_name(pr.name), pr.market)
        ln = lines.get(key)
        if not ln or not ln["over"] or not ln["under"]:
            continue
        if ln["teams"] and pr.team not in ln["teams"]:
            continue          # name collision across games
        line = ln["over"][0]
        p_over = rt.p_over(pr.pos, pr.market, pr.proj, line)
        if p_over is None:
            continue
        p_under = 1.0 - p_over
        side = "over" if p_over >= p_under else "under"
        p = max(p_over, p_under)
        _, price, book = ln[side]
        imp = M.implied(price)
        rows.append({
            "player_id": pr.player_id, "player": pr.name, "team": pr.team, "pos": pr.pos,
            "market": pr.market, "opponent": pr.opponent, "proj": pr.proj, "why": pr.why(),
            "line": line, "side": side, "p": p, "p_over": p_over, "price": price, "book": book,
            "implied": imp, "edge": p - imp, "games_cur": pr.games_cur,
            "injury": injuries.get(norm_name(pr.name)),
            "market_p_over": _market_p_over(ln),
        })
    return rows


def _market_p_over(ln: dict) -> float | None:
    """De-vigged market P(over) from the best over/under prices."""
    if not ln.get("over") or not ln.get("under"):
        return None
    io, iu = M.implied(ln["over"][1]), M.implied(ln["under"][1])
    return io / (io + iu) if (io + iu) > 0 else None


def pick_legs(rows: list[dict]) -> list[dict]:
    """Apply the bars, one leg per team, best edge first, cap MAX_LEGS."""
    ok = [r for r in rows
          if r["p"] >= BAR_P and r["edge"] >= BAR_EDGE and r["price"] >= MIN_PRICE
          and r["games_cur"] >= MIN_GAMES and r["injury"] not in ("Out", "Doubtful")
          and r["line"] >= MIN_LINE[r["market"]]]
    ok.sort(key=lambda r: -r["edge"])
    legs, teams = [], set()
    for r in ok:
        if r["team"] in teams:
            continue
        teams.add(r["team"])
        legs.append(r)
        if len(legs) >= MAX_LEGS:
            break
    return legs


def _dec(american: int) -> float:
    a = float(american)
    return 1 + a / 100 if a > 0 else 1 + 100 / -a


def parlay_price(legs: list[dict]) -> int:
    d = 1.0
    for r in legs:
        d *= _dec(r["price"])
    return int(round((d - 1) * 100)) if d >= 2 else int(round(-100 / (d - 1)))


def _fmt(p: int) -> str:
    return f"+{p}" if p > 0 else str(p)


MK_LABEL = {"pass": "Pass Yds", "rush": "Rush Yds", "rec": "Rec Yds"}


def build_week(week: int | None = None) -> tuple[int, list[dict], list[dict]]:
    """-> (week, all candidate rows, chosen legs) for the live slate."""
    week = week or current_week()
    cur = nfl_data.load_season("stats", SEASON)
    pri = nfl_data.load_season("stats", SEASON - 1)
    games = nfl_data.games_all()
    projs = M.project(cur, pri, week, games, SEASON)
    rows = evaluate(projs, tracker_lines(), ratio_table(), injury_gate(week))
    return week, rows, pick_legs(rows)


def slate_embed(week: int, rows: list[dict], legs: list[dict], dry: bool) -> discord.Embed:
    if not legs:
        e = discord.Embed(title=f"Week {week} — no parlay", color=0x95a5a6,
                          description=(f"{len(rows)} player-markets had lines; none cleared the bars "
                                       f"(P ≥ {BAR_P:.2f}, edge ≥ {BAR_EDGE:.2f}, price ≥ {MIN_PRICE})."))
        return e
    e = discord.Embed(title=f"Week {week} NFL parlay — {len(legs)} legs ({_fmt(parlay_price(legs))})",
                      color=0x2ecc71)
    for r in legs:
        flag = " ⚠️ Q" if r["injury"] == "Questionable" else ""
        e.add_field(
            name=f"{r['player']} {r['side'].upper()} {r['line']} {MK_LABEL[r['market']]} ({_fmt(r['price'])} {nflprops.BOOK_NAMES.get(r['book'], r['book'])}){flag}",
            value=f"model {r['p']:.0%} vs implied {r['implied']:.0%} (+{r['edge']*100:.1f} pts) • {r['why']}",
            inline=False)
    e.set_footer(text=("DRY RUN • " if dry else "") + f"{len(rows)} candidates • one leg per team • "
                 f"bars P≥{BAR_P:.2f} edge≥{BAR_EDGE:.2f} • graded Tuesdays in /nflrecord")
    return e


# ------------------------------------------------------------------ record

def store_legs(week: int, legs: list[dict]) -> None:
    ts = int(time.time())
    with _conn() as c:
        for r in legs:
            c.execute("INSERT OR REPLACE INTO nfl_parlay_legs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (SEASON, week, ts, r["player_id"], r["player"], r["team"], r["market"],
                       r["side"], r["line"], r["price"], r["book"], r["proj"], r["p"], r["edge"],
                       None, None))


def grade_legs() -> int:
    """Fill actual/result for posted legs whose box scores are in. -> graded count."""
    cur = nfl_data.load_season("stats", SEASON, force=True)
    if cur.empty:
        return 0
    idx = cur[cur["season_type"] == "REG"].set_index(["player_id", "week"])
    n = 0
    with _conn() as c:
        for wk, pid, mk, side, line in c.execute(
                "SELECT week, player_id, market, side, line FROM nfl_parlay_legs "
                "WHERE season=? AND result IS NULL", (SEASON,)).fetchall():
            if (pid, wk) not in idx.index:
                continue
            a = idx.loc[(pid, wk)]
            a = a.iloc[0] if isinstance(a, pd.DataFrame) else a
            y = float(a[M.MARKETS[mk][1]])
            res = "win" if (y > line if side == "over" else y < line) else "loss"
            c.execute("UPDATE nfl_parlay_legs SET actual=?, result=? WHERE season=? AND week=? "
                      "AND player_id=? AND market=?", (y, res, SEASON, wk, pid, mk))
            n += 1
    return n


def record_text() -> str:
    with _conn() as c:
        rows = c.execute("SELECT week, player, market, side, line, price, p, actual, result "
                         "FROM nfl_parlay_legs WHERE season=? AND player_id != '__none__' "
                         "ORDER BY week, player", (SEASON,)).fetchall()
    if not rows:
        return "No legs posted yet this season."
    units = 0.0
    w = l = 0
    by_week: dict[int, list] = {}
    for wk, pl, mk, side, line, price, p, actual, res in rows:
        by_week.setdefault(wk, []).append((pl, mk, side, line, price, p, actual, res))
        if res == "win":
            w += 1
            units += _dec(price) - 1
        elif res == "loss":
            l += 1
            units -= 1
    out = [f"Legs {w}-{l} • {units:+.2f}u at 1u/leg (flat) • parlays: "]
    pw = pl_ = 0
    for wk, legs in by_week.items():
        res = [x[7] for x in legs]
        if all(r == "win" for r in res):
            pw += 1
        elif "loss" in res:
            pl_ += 1
    out[0] += f"{pw}-{pl_}"
    for wk, legs in by_week.items():
        out.append(f"\nWeek {wk}")
        for pl, mk, side, line, price, p, actual, res in legs:
            mark = {"win": "✅", "loss": "❌"}.get(res, "⏳")
            act = f" → {actual:.0f}" if actual is not None else ""
            out.append(f"{mark} {pl} {side} {line} {MK_LABEL[mk]} ({_fmt(price)}) model {p:.0%}{act}")
    return "\n".join(out)


# ------------------------------------------------------------------ posting

_wh_cache: dict[int, object] = {}


async def _post(bot, embed: discord.Embed) -> bool:
    ch = bot.get_channel(CHANNEL_ID)
    if not ch:
        log.warning("nflparlay: channel %d not found", CHANNEL_ID)
        return False
    wh = _wh_cache.get(CHANNEL_ID)
    if wh is None:
        try:
            hooks = await ch.webhooks()
            wh = next((h for h in hooks if h.name == WEBHOOK_NAME), None) or await ch.create_webhook(name=WEBHOOK_NAME)
        except Exception:
            wh = False
        _wh_cache[CHANNEL_ID] = wh
    try:
        if wh:
            await wh.send(embed=embed, username=DISPLAY_NAME)
        else:
            await ch.send(embed=embed)
        return True
    except Exception:
        log.exception("nflparlay post failed")
        return False


def _already_posted(week: int) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM nfl_parlay_legs WHERE season=? AND week=?",
                         (SEASON, week)).fetchone() is not None


async def weekly_task(bot):
    await bot.wait_until_ready()
    try:
        await asyncio.to_thread(ratio_table)
    except Exception:
        log.exception("ratio table fit failed — parlays off until it succeeds")
    hh, mm = (int(x) for x in POST_ET.split(":"))
    log.info("nflparlay: weekly post %s (Wed %s ET) • bars P≥%.2f edge≥%.2f price≥%d • record grading Tue",
             f"-> channel {CHANNEL_ID}" if CHANNEL_ID else "OFF (no NFL_PARLAY_CHANNEL_ID; /nflparlay only)",
             POST_ET, BAR_P, BAR_EDGE, MIN_PRICE)
    graded_day = None
    while not bot.is_closed():
        now = datetime.now(ET)
        try:
            if now.weekday() == 1 and graded_day != now.date():       # Tuesday: grade last week
                n = await asyncio.to_thread(grade_legs)
                graded_day = now.date()
                if n:
                    log.info("nflparlay: graded %d leg(s)", n)
            if CHANNEL_ID and now.weekday() == 2 and (now.hour, now.minute) >= (hh, mm):
                wk = await asyncio.to_thread(current_week)
                if not _already_posted(wk):
                    week, rows, legs = await asyncio.to_thread(build_week, wk)
                    ok = await _post(bot, slate_embed(week, rows, legs, dry=False))
                    if ok and legs:
                        store_legs(week, legs)
                    if ok and not legs:
                        # remember the no-parlay verdict so we don't repost all day
                        with _conn() as c:
                            c.execute("INSERT OR IGNORE INTO nfl_parlay_legs VALUES "
                                      "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                      (SEASON, week, int(time.time()), "__none__", "no parlay", "",
                                       "pass", "", 0, 0, "", 0, 0, 0, None, "skip"))
        except Exception:
            log.exception("nflparlay weekly task failed")
        await asyncio.sleep(600)


# ------------------------------------------------------------------ backtest

BT_MARKETS = ",".join(M.ODDS_MARKET.values())


def _snapshot_iso(kick_utc: datetime, hours_before: float = 1.0) -> str:
    return (kick_utc - timedelta(hours=hours_before)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def backtest(season: int, progress) -> str:
    """Grade the model on `season` with real pre-kick lines. `progress(text)`
    is awaited with running status. Returns the receipts text."""
    cur = nfl_data.load_season("stats", season)
    pri = nfl_data.load_season("stats", season - 1)
    fit_cur = nfl_data.load_season("stats", season - 1)
    fit_pri = nfl_data.load_season("stats", season - 2)
    games = nfl_data.games_all()
    if cur.empty or pri.empty or fit_pri.empty:
        return f"Missing nflverse seasons for {season} (need {season - 2}..{season})."
    await progress(f"fitting ratio table on {season - 1} (prior {season - 2})…")
    rt = await asyncio.to_thread(M.RatioTable().fit, fit_cur, fit_pri, games, season - 1)
    actual = cur[cur["season_type"] == "REG"].set_index(["player_id", "week"])
    sched = games[(games["season"] == season) & (games["game_type"] == "REG")]
    cal_rows, mkt_rows, legs_all = [], [], []
    units = 0.0
    w = l = 0
    pw = pl_ = 0
    spent0 = nfl_odds.credits_spent()
    for wk in range(3, 19):
        wkg = sched[sched["week"] == wk]
        if wkg.empty:
            continue
        projs = await asyncio.to_thread(M.project, cur, pri, wk, games, season)
        # one events listing at the week's first kickoff
        first = wkg.sort_values("gameday").iloc[0]
        kick0 = datetime.fromisoformat(f"{first['gameday']}T{first['gametime']}").replace(tzinfo=ET).astimezone(timezone.utc)
        events = await asyncio.to_thread(nfl_odds.get_historical_events, _snapshot_iso(kick0, 24))
        ev_by_teams = {}
        for ev in events:
            h, a = _team_from_full(ev.get("home_team")), _team_from_full(ev.get("away_team"))
            if h and a:
                ev_by_teams[(h, a)] = ev
        lines: dict[tuple[str, str], dict] = {}
        for _, g in wkg.iterrows():
            ev = ev_by_teams.get((g["home_team"], g["away_team"]))
            if not ev:
                continue
            kick = datetime.fromisoformat(f"{g['gameday']}T{g['gametime']}").replace(tzinfo=ET).astimezone(timezone.utc)
            data = await asyncio.to_thread(nfl_odds.get_historical_event_props, ev["id"], BT_MARKETS,
                                           _snapshot_iso(kick, 1))
            if not data:
                continue
            for (odds_mk, player, book), q in nflprops.extract_quotes(data).items():
                mk = next(k for k, v in M.ODDS_MARKET.items() if v == odds_mk)
                if q.get("line") is None:
                    continue
                e = lines.setdefault((norm_name(player), mk), {"over": None, "under": None,
                                                              "teams": {g["home_team"], g["away_team"]}})
                for side in ("over", "under"):
                    pr_ = q.get(side)
                    if pr_ is not None and (e[side] is None or pr_ > e[side][1]):
                        e[side] = (float(q["line"]), int(pr_), book)
        rows = evaluate(projs, lines, rt)
        graded = []
        for r in rows:
            key = (r["player_id"], wk)
            if key not in actual.index:
                continue
            a = actual.loc[key]
            a = a.iloc[0] if isinstance(a, pd.DataFrame) else a
            y = float(a[M.MARKETS[r["market"]][1]])
            r["actual"] = y
            r["hit"] = 1 if (y > r["line"] if r["side"] == "over" else y < r["line"]) else 0
            cal_rows.append((r["p_over"], 1 if y > r["line"] else 0))
            if r["market_p_over"] is not None:
                mkt_rows.append((r["market_p_over"], 1 if y > r["line"] else 0))
            graded.append(r)
        legs = pick_legs(graded)
        for r in legs:
            legs_all.append((wk, r))
            if r["hit"]:
                w += 1
                units += _dec(r["price"]) - 1
            else:
                l += 1
                units -= 1
        if legs:
            if all(r["hit"] for r in legs):
                pw += 1
            else:
                pl_ += 1
        await progress(f"week {wk}: {len(rows)} lined, {len(graded)} graded, {len(legs)} legs → "
                       f"legs {w}-{l} {units:+.1f}u, parlays {pw}-{pl_} • "
                       f"{nfl_odds.credits_spent() - spent0} credits")
    out = [f"NFL backtest {season} — model fit {season - 1}/{season - 2}, lines = best price ~1h pre-kick",
           f"credits used: {nfl_odds.credits_spent() - spent0}", "",
           "MODEL calibration (P(over) vs actual over, ALL lined player-markets):",
           M.calibration_table(cal_rows), "",
           "MARKET calibration (de-vigged P(over), same rows):",
           M.calibration_table(mkt_rows), "",
           f"LEGS at the bars (P≥{BAR_P:.2f}, edge≥{BAR_EDGE:.2f}, price≥{MIN_PRICE}, 1 per team, ≤{MAX_LEGS}):",
           f"  {w}-{l}  {units:+.2f}u flat 1u/leg  ({(w / (w + l)):.1%} hit)" if (w + l) else "  no legs cleared",
           f"PARLAYS (all legs must hit): {pw}-{pl_}"]
    if legs_all:
        out.append("")
        out.append("legs by week:")
        for wk, r in legs_all:
            out.append(f"  W{wk} {'✅' if r['hit'] else '❌'} {r['player']} {r['side']} {r['line']} "
                       f"{MK_LABEL[r['market']]} ({_fmt(r['price'])}) model {r['p']:.0%} → {r['actual']:.0f}")
    return "\n".join(out)


# ------------------------------------------------------------------ commands

def _guarded(fn):
    import functools

    @functools.wraps(fn)
    async def run(interaction, *a, **k):
        try:
            await fn(interaction, *a, **k)
        except Exception as e:
            log.exception("nfl command %s failed", fn.__name__)
            msg = f"⚠️ Command failed — {type(e).__name__}: {e}"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg[:1900])
                else:
                    await interaction.response.send_message(msg[:1900])
            except Exception:
                pass
    return run


DESC = {
    "nflparlay": "Build this week's NFL parlay from the model + tracked lines (dry run unless post)",
    "nflparlay.post": "Post it to the parlay channel and log the legs (default: dry run)",
    "nflrecord": "Forward log: every posted NFL parlay leg, graded, with units",
    "nflbacktest": "Grade the model on a past season with real pre-kick lines (~8.5K credits)",
    "nflbacktest.season": "Season to grade (default: last season)",
}


def _chunks(text: str, n: int = 1900):
    while text:
        yield text[:n]
        text = text[n:]


def setup(bot):
    tree = bot.tree

    @tree.command(name="nflparlay", description=DESC["nflparlay"])
    @app_commands.describe(post=DESC["nflparlay.post"])
    @_guarded
    async def nflparlay_cmd(interaction: discord.Interaction, post: bool = False):
        await interaction.response.defer()
        week, rows, legs = await asyncio.to_thread(build_week)
        if post and not CHANNEL_ID:
            await interaction.followup.send("NFL_PARLAY_CHANNEL_ID isn't set — showing a dry run instead.")
            post = False
        emb = slate_embed(week, rows, legs, dry=not post)
        if post:
            ok = await _post(bot, emb)
            if ok and legs:
                store_legs(week, legs)
            await interaction.followup.send("Posted." if ok else "Post failed — check the log.")
        else:
            await interaction.followup.send(embed=emb)

    @tree.command(name="nflrecord", description=DESC["nflrecord"])
    @_guarded
    async def nflrecord_cmd(interaction: discord.Interaction):
        await interaction.response.defer()
        await asyncio.to_thread(grade_legs)
        txt = record_text()
        for ch in _chunks(txt):
            await interaction.followup.send(f"```\n{ch}\n```")

    @tree.command(name="nflbacktest", description=DESC["nflbacktest"])
    @app_commands.describe(season=DESC["nflbacktest.season"])
    @_guarded
    async def nflbacktest_cmd(interaction: discord.Interaction, season: int | None = None):
        await interaction.response.defer()
        season = season or SEASON - 1
        msg = await interaction.followup.send(f"NFL backtest {season} starting…", wait=True)
        lines_: list[str] = []

        async def progress(t: str):
            lines_.append(t)
            try:
                await msg.edit(content="```\n" + "\n".join(lines_[-12:]) + "\n```")
            except Exception:
                pass
        txt = await backtest(season, progress)
        for ch in _chunks(txt):
            await interaction.followup.send(f"```\n{ch}\n```")

    log.info("nflparlay: registered /nflparlay /nflrecord /nflbacktest")


def start(bot):
    if getattr(bot, "_nflparlay_task", None) is None:
        bot._nflparlay_task = bot.loop.create_task(weekly_task(bot))
