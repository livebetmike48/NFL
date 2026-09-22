"""
NFL PARLAY v2 -- Bot Cooks for football.

Every ticket is built from REAL numbers and shows its work on the leg:
projection arithmetic + the matchup. No "model %" on the embed. The bars
below are what a leg has to clear; they're thresholds, not a score.

KINDS (one command, one choice):
  general   any market, overs or unders, incl. anytime TD
  pass / rush / rec / recs   one market
  td        anytime TD legs only (Yes side)
  alt       alternate lines at plus money ("80+ yards at +200"), min/max odds
  reverse   same player UNDER receptions + OVER rec yds -- big-play profile
            (high aDOT / yards per catch) vs a defense that gives up chunks
  sgp       one game (or the primetime games): any market, no team cap

BOOKS: every ticket is placeable on ONE book, FanDuel or DraftKings; the
bot builds both and posts the stronger one, with a betslip link per leg
(Odds API deep links, includeLinks=true).

LINES: fetched on demand for the slate -- 9 markets x ~16 games ~= 144
credits per build; cached 15 min so the four Sunday tickets share one fetch.

SCHEDULE (ET) -- driven by the actual kickoffs, not fixed clock times:
  DAY slate    = games kicking 11am-6pm ET on a day. Posts NFL_LEAD_MIN
                 (default 75) before the FIRST day kickoff -> 1pm = 11:45.
                 SUNDAY gets the four tickets (general, rush, rec, td);
                 any other day (late-season Saturday, Thanksgiving,
                 Christmas) gets ONE general ticket.
  EVENING slate = games kicking 6pm or later (TNF, SNF, MNF, Sat night).
                 ONE ticket, NFL_PRIME_LEAD_MIN (default 30) before the
                 first evening kickoff: a Same Game Parlay if one game,
                 a Primetime Parlay drawing from all of them if several.
  Early games (before 11am ET -- London/Germany) are never on a ticket.
  NFL_RECAP_POST_ET  default "Tue 10:00"  -> graded recap of last week.
Posting needs NFL_PARLAY_CHANNEL_ID; otherwise everything is command-only.

Every posted leg is stored and graded against nflverse box scores.
/nflrecord [kind] = forward log by kind. /nflparlay kind [game] [post]
[min_odds] [max_odds] = build now (dry run unless post).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
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
CHANNEL_ID = int(os.getenv("NFL_PARLAY_CHANNEL_ID", "0") or 0)
LEAD_MIN = int(os.getenv("NFL_LEAD_MIN", "75") or 75)           # day slate: minutes before 1st kick
PRIME_LEAD_MIN = int(os.getenv("NFL_PRIME_LEAD_MIN", "30") or 30)  # evening: minutes before 1st kick
RECAP_ET = os.getenv("NFL_RECAP_POST_ET", "Tue 10:00")
DAY_START_H, EVENING_H = 11, 18     # ET: <11am = early (skipped), >=6pm = evening
WEBHOOK_NAME = "LBM NFL Parlay"
DISPLAY_NAME = os.getenv("NFL_PARLAY_NAME", "NFL Parlay")
BOOKS = ["fanduel", "draftkings"]
BOOK_NAMES = {"fanduel": "FanDuel", "draftkings": "DraftKings"}
DB = nflprops.DB
RATIO_PATH = os.path.join(nfl_data.DATA_DIR, f"ratio_table_{SEASON - 1}_v2.json")
FETCH_TTL = 15 * 60
MIN_GAMES = int(os.getenv("NFL_MIN_GAMES", "2"))
MAX_DISAGREE = float(os.getenv("NFL_MAX_DISAGREE", "0.35"))
MIN_LINE = {"pass": 150.0, "rush": 20.0, "rec": 15.0, "recs": 1.5}

# ------------------------------------------------------------------ bars
# kind -> dict(markets, sides, p, edge, min_price, max_price, max_legs,
#              min_legs, per_team, alt_only)
KINDS = {
    "general": dict(markets=["pass", "rush", "rec", "recs", "td"], sides=["over", "under", "yes"],
                    p=0.60, edge=0.05, min_price=-150, max_price=400, max_legs=6, min_legs=2, per_team=1),
    "pass":    dict(markets=["pass"], sides=["over", "under"], p=0.60, edge=0.05, min_price=-150,
                    max_price=400, max_legs=5, min_legs=2, per_team=1),
    "rush":    dict(markets=["rush"], sides=["over", "under"], p=0.60, edge=0.05, min_price=-150,
                    max_price=400, max_legs=5, min_legs=2, per_team=1),
    "rec":     dict(markets=["rec"], sides=["over", "under"], p=0.60, edge=0.05, min_price=-150,
                    max_price=400, max_legs=5, min_legs=2, per_team=1),
    "recs":    dict(markets=["recs"], sides=["over", "under"], p=0.60, edge=0.05, min_price=-150,
                    max_price=400, max_legs=5, min_legs=2, per_team=1),
    "td":      dict(markets=["td"], sides=["yes"], p=0.40, edge=0.05, min_price=-140,
                    max_price=400, max_legs=5, min_legs=2, per_team=1),
    "alt":     dict(markets=["pass", "rush", "rec"], sides=["over"], p=0.35, edge=0.05, min_price=150,
                    max_price=400, max_legs=4, min_legs=2, per_team=1, alt_only=True),
    "sgp":     dict(markets=["pass", "rush", "rec", "recs", "td"], sides=["over", "under", "yes"],
                    p=0.58, edge=0.04, min_price=-150, max_price=400, max_legs=5, min_legs=2, per_team=99),
}
KINDS["prime"] = KINDS["sgp"]        # several evening games: same bars, legs from any of them
KIND_LABEL = {"general": "Parlay", "pass": "Passing Yards Parlay", "rush": "Rushing Yards Parlay",
              "rec": "Receiving Yards Parlay", "recs": "Receptions Parlay", "td": "Anytime TD Parlay",
              "alt": "Alt-Line Parlay", "reverse": "Big-Play Parlay", "sgp": "Same Game Parlay",
              "prime": "Primetime Parlay"}
# reverse (same-player pairs) has its own bars
REVERSE = dict(p=0.50, edge=0.02, min_price=-150, max_pairs=3, adot=11.0, ypr=13.0, min_rec=5)

# ------------------------------------------------------------------ storage

def _conn():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS nfl_parlay_legs (
        season INTEGER, week INTEGER, kind TEXT, ticket TEXT, posted_ts INTEGER,
        player_id TEXT, player TEXT, team TEXT, market TEXT, side TEXT, line REAL,
        price INTEGER, book TEXT, proj REAL, p REAL, edge REAL,
        actual REAL, result TEXT,
        PRIMARY KEY (season, ticket, player_id, market, side, line))""")
    return c


# ------------------------------------------------------------------ names / teams

_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?")


def norm_name(s: str) -> str:
    s = (s or "").lower().replace(".", "").replace("'", "").replace("-", " ")
    return " ".join(_SUFFIX.sub("", s).split())


def team_from_full(full: str) -> str | None:
    f = (full or "").lower()
    for ab, nick in nfl_data.TEAMS.items():
        if nick.lower() in f:
            return ab
    return None


# ------------------------------------------------------------------ ratio table

_ratio: M.RatioTable | None = None


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
    fit, pri = SEASON - 1, SEASON - 2
    _ratio = M.RatioTable().fit(nfl_data.load_season("stats", fit), nfl_data.load_season("stats", pri),
                                nfl_data.games_all(), fit, snaps=nfl_data.load_season("snaps", fit))
    try:
        with open(RATIO_PATH, "w") as f:
            json.dump(_ratio.to_dict(), f)
    except Exception:
        log.exception("ratio table cache write failed")
    log.info("nflparlay: ratio table fit on %d (prior %d) in %.0fs\n%s", fit, pri, time.time() - t0,
             _ratio.summary())
    return _ratio


# ------------------------------------------------------------------ slate + quotes

def current_week() -> int:
    g = nfl_data.load("games")
    g = g[g["game_type"] == "REG"]
    open_ = g[g["result"].isna()]
    return int(open_["week"].min()) if not open_.empty else int(g["week"].max())


def slate_events(now: int | None = None) -> list[dict]:
    """Tracker events still to kick off this week: [{id, home, away, kick}]."""
    now = now or int(time.time())
    out = []
    for ev, (kick, home, away) in nflprops._events_map().items():
        if kick >= now and ev in nflprops.active_event_ids(now):
            out.append({"id": ev, "home": team_from_full(home), "away": team_from_full(away),
                        "kick": kick, "home_full": home, "away_full": away})
    return sorted(out, key=lambda e: e["kick"])


def _slot(kick: int) -> str:
    h = datetime.fromtimestamp(kick, ET).hour
    return "early" if h < DAY_START_H else "evening" if h >= EVENING_H else "day"


def day_slate(now: int | None = None, slot: str = "day") -> list[dict]:
    """Today's (ET) still-to-kick games in one slot: 'day' = 11am-6pm,
    'evening' = 6pm+. Early games are never returned."""
    now = now or int(time.time())
    today = datetime.fromtimestamp(now, ET).date()
    return [e for e in slate_events(now)
            if datetime.fromtimestamp(e["kick"], ET).date() == today and _slot(e["kick"]) == slot]


def primetime_events(now: int | None = None) -> list[dict]:
    return day_slate(now, "evening")


_quote_cache: dict[str, tuple[float, list[dict]]] = {}
FETCH_MARKETS = ",".join(list(M.ODDS_MARKET.values()) + list(M.ODDS_ALT.values()) + [M.ODDS_TD])


def fetch_quotes(events: list[dict]) -> list[dict]:
    """Every FD/DK quote on the alert+alt+TD markets for these events, with
    deep links. -> [{event, teams, book, market(model key), alt, player,
    line, side, price, link}]"""
    out = []
    alt_of = {v: k for k, v in M.ODDS_ALT.items()}
    main_of = {v: k for k, v in M.ODDS_MARKET.items()}
    for e in events:
        c = _quote_cache.get(e["id"])
        if c and time.time() - c[0] < FETCH_TTL:
            out.extend(c[1]); continue
        data = nfl_odds.get_event_props(e["id"], FETCH_MARKETS, ",".join(BOOKS), links=True)
        rows = []
        for bm in (data or {}).get("bookmakers", []):
            bk = (bm.get("key") or "").lower()
            if bk not in BOOKS:
                continue
            for mk in bm.get("markets", []):
                key = mk.get("key")
                if key == M.ODDS_TD:
                    model_mk, alt = "td", False
                elif key in main_of:
                    model_mk, alt = main_of[key], False
                elif key in alt_of:
                    model_mk, alt = alt_of[key], True
                else:
                    continue
                for oc in mk.get("outcomes", []):
                    player = oc.get("description") or ""
                    side = (oc.get("name") or "").lower()
                    if not player or side not in ("over", "under", "yes"):
                        continue
                    rows.append({"event": e["id"], "teams": {e["home"], e["away"]},
                                 "game": f"{e['away']} @ {e['home']}", "kick": e["kick"],
                                 "book": bk, "market": model_mk, "alt": alt, "player": player,
                                 "line": oc.get("point"), "side": side, "price": oc.get("price"),
                                 "link": oc.get("link") or mk.get("link") or bm.get("link")})
        _quote_cache[e["id"]] = (time.time(), rows)
        out.extend(rows)
    return out


# ------------------------------------------------------------------ candidates

def injury_gate(week: int) -> dict[str, str]:
    inj = nfl_data.load("injuries")
    if inj.empty:
        return {}
    inj = inj[inj["season"] == SEASON]
    wk = inj[inj["week"] == week]
    if wk.empty:
        wk = inj[inj["week"] == inj["week"].max()]
    return {norm_name(r["full_name"]): str(r["report_status"]) for _, r in wk.iterrows()
            if str(r.get("report_status") or "") in ("Out", "Doubtful", "Questionable")}


def big_play_profile() -> dict[str, tuple[float, float]]:
    """norm_name -> (aDOT, yards per reception) this season, min REVERSE['min_rec']."""
    st = nfl_data.load_season("stats", SEASON)
    st = st[st["season_type"] == "REG"]
    g = st.groupby("player_display_name")[["targets", "receptions", "receiving_yards", "receiving_air_yards"]].sum()
    g = g[g["receptions"] >= REVERSE["min_rec"]]
    return {norm_name(n): (float(r["receiving_air_yards"] / r["targets"]) if r["targets"] else 0.0,
                           float(r["receiving_yards"] / r["receptions"]))
            for n, r in g.iterrows()}


def candidates(week: int, events: list[dict]) -> list[dict]:
    """Every quote joined to a projection -> leg candidate with p, edge."""
    cur = nfl_data.load_season("stats", SEASON)
    pri = nfl_data.load_season("stats", SEASON - 1)
    projs = M.project(cur, pri, week, nfl_data.games_all(), SEASON, nfl_data.load_season("snaps", SEASON))
    by_key = {(norm_name(p.name), p.market): p for p in projs}
    rt = ratio_table()
    inj = injury_gate(week)
    out = []
    for q in fetch_quotes(events):
        pr = by_key.get((norm_name(q["player"]), q["market"]))
        if pr is None or pr.team not in q["teams"] or q["price"] is None:
            continue
        if q["market"] == "td":
            if q["side"] != "yes":
                continue
            p = M.p_anytime_td(pr.proj)
            line = None
        else:
            if q["line"] is None:
                continue
            line = float(q["line"])
            p_over = rt.p_over(pr.pos, pr.market, pr.proj, line)
            if p_over is None:
                continue
            p = p_over if q["side"] == "over" else 1.0 - p_over
        imp = M.implied(q["price"])
        out.append({**q, "line": line, "player_id": pr.player_id, "name": pr.name, "team": pr.team,
                    "pos": pr.pos, "proj": pr.proj, "why": pr.why(), "matchup": pr.matchup(),
                    "p": p, "implied": imp, "edge": p - imp, "games_cur": pr.games_cur,
                    "injury": inj.get(norm_name(pr.name))})
    return out


def _passes(r: dict, k: dict, min_price: int | None, max_price: int | None) -> bool:
    lo = min_price if min_price is not None else k["min_price"]
    hi = max_price if max_price is not None else k["max_price"]
    if r["market"] not in k["markets"] or r["side"] not in k["sides"]:
        return False
    if k.get("alt_only") and not r["alt"]:
        return False
    if not k.get("alt_only") and r["alt"]:
        return False          # alt lines only on the alt ticket
    if r["injury"] in ("Out", "Doubtful") or r["games_cur"] < MIN_GAMES:
        return False
    if r["market"] != "td":
        if r["line"] < MIN_LINE[r["market"]]:
            return False
        if abs(r["line"] - r["proj"]) / max(r["line"], 1.0) > MAX_DISAGREE and not r["alt"]:
            return False
    return r["p"] >= k["p"] and r["edge"] >= k["edge"] and lo <= r["price"] <= hi


def build_ticket(kind: str, cands: list[dict], book: str, min_price=None, max_price=None,
                 events: set[str] | None = None) -> list[dict]:
    k = KINDS[kind]
    ok = [r for r in cands if r["book"] == book and _passes(r, k, min_price, max_price)
          and (events is None or r["event"] in events)]
    ok.sort(key=lambda r: -r["edge"])
    legs, teams, players = [], {}, set()
    for r in ok:
        if teams.get(r["team"], 0) >= k["per_team"] or r["player_id"] in players:
            continue
        teams[r["team"]] = teams.get(r["team"], 0) + 1
        players.add(r["player_id"])
        legs.append(r)
        if len(legs) >= k["max_legs"]:
            break
    return legs if len(legs) >= k["min_legs"] else []


def build_reverse(cands: list[dict], book: str) -> list[dict]:
    """Pairs: same player UNDER receptions + OVER rec yds, big-play profile."""
    prof = big_play_profile()
    by_p: dict[str, dict] = {}
    for r in cands:
        if r["book"] != book or r["alt"] or r["injury"] in ("Out", "Doubtful"):
            continue
        if r["market"] == "recs" and r["side"] == "under":
            by_p.setdefault(r["player_id"], {})["u"] = r
        elif r["market"] == "rec" and r["side"] == "over":
            by_p.setdefault(r["player_id"], {})["o"] = r
    pairs = []
    for pid, d in by_p.items():
        if "u" not in d or "o" not in d:
            continue
        u, o = d["u"], d["o"]
        adot, ypr = prof.get(norm_name(u["name"]), (0.0, 0.0))
        if adot < REVERSE["adot"] and ypr < REVERSE["ypr"]:
            continue
        if min(u["p"], o["p"]) < REVERSE["p"] or min(u["edge"], o["edge"]) < REVERSE["edge"]:
            continue
        if min(u["price"], o["price"]) < REVERSE["min_price"]:
            continue
        u = {**u, "profile": f"aDOT {adot:.1f} • {ypr:.1f} yds/catch"}
        pairs.append((u["edge"] + o["edge"], u, o))
    pairs.sort(key=lambda x: -x[0])
    legs = []
    seen = set()
    for _, u, o in pairs[:REVERSE["max_pairs"]]:
        if u["team"] in seen:
            continue
        seen.add(u["team"])
        legs += [u, o]
    return legs if len(legs) >= 2 else []


def best_book(kind: str, cands: list[dict], **kw) -> tuple[str | None, list[dict]]:
    best = (None, [])
    for bk in BOOKS:
        legs = build_reverse(cands, bk) if kind == "reverse" else build_ticket(kind, cands, bk, **kw)
        if len(legs) > len(best[1]) or (len(legs) == len(best[1]) and legs and
                                        parlay_price(legs) > parlay_price(best[1])):
            best = (bk, legs)
    return best


# ------------------------------------------------------------------ odds math

def _dec(american: int) -> float:
    a = float(american)
    return 1 + a / 100 if a > 0 else 1 + 100 / -a


def parlay_price(legs: list[dict]) -> int:
    if not legs:
        return 0
    d = 1.0
    for r in legs:
        d *= _dec(r["price"])
    return int(round((d - 1) * 100)) if d >= 2 else int(round(-100 / (d - 1)))


def _fmt(p: int) -> str:
    return f"+{p}" if p > 0 else str(p)


# ------------------------------------------------------------------ embed

def _leg_line(r: dict) -> tuple[str, str]:
    lbl = M.MK_LABEL[r["market"]]
    if r["market"] == "td":
        head = f"{r['name']} Anytime TD ({_fmt(r['price'])})"
    else:
        head = f"{r['name']} {r['side'].upper()} {r['line']:g} {lbl} ({_fmt(r['price'])})"
    if r.get("injury") == "Questionable":
        head += " ⚠️ Q"
    body = f"{r['game']} • {r['why']}"
    if r.get("matchup"):
        body += f"\n{r['matchup']}"
    if r.get("profile"):
        body += f" • {r['profile']}"
    if r.get("link"):
        body += f" • [bet]({r['link']})"
    return head, body


def ticket_embed(kind: str, week: int, book: str | None, legs: list[dict], dry: bool,
                 note: str = "") -> discord.Embed:
    title_kind = KIND_LABEL[kind]
    if not legs:
        return discord.Embed(title=f"Week {week} {title_kind} — no ticket", color=0x95a5a6,
                             description=f"Nothing cleared the bars{note}.")
    e = discord.Embed(title=f"Week {week} {title_kind} — {len(legs)} legs {_fmt(parlay_price(legs))} "
                            f"on {BOOK_NAMES[book]}", color=0x2ecc71)
    for r in legs:
        h, b = _leg_line(r)
        e.add_field(name=h[:256], value=b[:1024], inline=False)
    foot = ("DRY RUN • " if dry else "") + f"all legs {BOOK_NAMES[book]} • price = straight product"
    if kind in ("sgp", "reverse"):
        foot += " (the book's SGP price will differ)"
    if kind == "reverse":
        foot += " • fewer catches + more yards = big-play profile; legs pull against each other"
    e.set_footer(text=foot + " • graded Tuesdays • /nflrecord")
    return e


# ------------------------------------------------------------------ build

def build(kind: str, week: int | None = None, game: str | None = None, prime: bool = False,
          min_price: int | None = None, max_price: int | None = None,
          events: list[dict] | None = None) -> tuple[int, str | None, list[dict], str]:
    """-> (week, book, legs, note). game = team text to restrict to one game;
    events = an explicit slate (the scheduler passes today's day/evening games)."""
    week = week or current_week()
    if events is None:
        events = primetime_events() if prime else [e for e in slate_events() if _slot(e["kick"]) != "early"]
    if kind == "prime" and len(events) == 1:
        kind = "sgp"
    note = ""
    if game:
        t = nfl_data.resolve_team(game)
        events = [e for e in events if t in (e["home"], e["away"])]
        if not events:
            return week, None, [], f" — no upcoming game for {game}"
    if kind == "sgp" and not game and not prime and len(events) > 1:
        events = events[:1]                          # next kickoff
    if not events:
        return week, None, [], " — no games in the window"
    cands = candidates(week, events)
    ev_ids = {e["id"] for e in events}
    book, legs = best_book(kind, cands, min_price=min_price, max_price=max_price, events=ev_ids) \
        if kind != "reverse" else best_book(kind, cands)
    if not legs:
        note = f" — {len(cands)} priced legs looked at"
    return week, book, legs, note


# ------------------------------------------------------------------ record

def store(kind: str, week: int, book: str, legs: list[dict]) -> str:
    ticket = f"{SEASON}-W{week}-{kind}-{int(time.time())}"
    with _conn() as c:
        for r in legs:
            c.execute("INSERT OR REPLACE INTO nfl_parlay_legs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (SEASON, week, kind, ticket, int(time.time()), r["player_id"], r["name"], r["team"],
                       r["market"], r["side"], r["line"], r["price"], book, r["proj"], r["p"], r["edge"],
                       None, None))
    return ticket


def grade() -> int:
    cur = nfl_data.load_season("stats", SEASON, force=True)
    if cur.empty:
        return 0
    idx = cur[cur["season_type"] == "REG"].set_index(["player_id", "week"])
    n = 0
    with _conn() as c:
        rows = c.execute("SELECT rowid, week, player_id, market, side, line FROM nfl_parlay_legs "
                         "WHERE season=? AND result IS NULL", (SEASON,)).fetchall()
        for rid, wk, pid, mk, side, line in rows:
            if (pid, wk) not in idx.index:
                continue
            a = idx.loc[(pid, wk)]
            a = a.iloc[0] if isinstance(a, pd.DataFrame) else a
            if mk == "td":
                y = float((a["rushing_tds"] or 0) + (a["receiving_tds"] or 0))
                win = y >= 1
            else:
                y = float(a[M.MARKETS[mk][1]])
                win = y > line if side == "over" else y < line
            c.execute("UPDATE nfl_parlay_legs SET actual=?, result=? WHERE rowid=?",
                      (y, "win" if win else "loss", rid))
            n += 1
    return n


def record_text(kind: str | None = None, week: int | None = None) -> str:
    q = "SELECT week, kind, ticket, player, market, side, line, price, actual, result FROM nfl_parlay_legs WHERE season=?"
    args: list = [SEASON]
    if kind:
        q += " AND kind=?"; args.append(kind)
    if week:
        q += " AND week=?"; args.append(week)
    with _conn() as c:
        rows = c.execute(q + " ORDER BY week, kind, ticket, player", args).fetchall()
    if not rows:
        return "No legs posted yet."
    tickets: dict[str, list] = {}
    for r in rows:
        tickets.setdefault(r[2], []).append(r)
    by_kind: dict[str, dict] = {}
    for tk, legs in tickets.items():
        kd = legs[0][1]
        s = by_kind.setdefault(kd, {"w": 0, "l": 0, "u": 0.0, "pw": 0, "pl": 0, "open": 0})
        res = [x[9] for x in legs]
        for wk_, kd_, t_, pl, mk, side, line, price, actual, r in legs:
            if r == "win":
                s["w"] += 1; s["u"] += _dec(price) - 1
            elif r == "loss":
                s["l"] += 1; s["u"] -= 1
        if all(x == "win" for x in res):
            s["pw"] += 1
        elif "loss" in res:
            s["pl"] += 1
        else:
            s["open"] += 1
    out = ["kind        tickets   legs      units (1u/leg)"]
    for kd, s in by_kind.items():
        out.append(f"{KIND_LABEL[kd]:<24}{s['pw']}-{s['pl']}{' +' + str(s['open']) + ' open' if s['open'] else '':<10} "
                   f"{s['w']}-{s['l']}   {s['u']:+.2f}u")
    for tk, legs in tickets.items():
        wk_, kd = legs[0][0], legs[0][1]
        res = [x[9] for x in legs]
        mark = "✅" if all(x == "win" for x in res) else "❌" if "loss" in res else "⏳"
        out.append(f"\n{mark} Week {wk_} {KIND_LABEL[kd]}")
        for _, _, _, pl, mk, side, line, price, actual, r in legs:
            m = {"win": "✅", "loss": "❌"}.get(r, "⏳")
            lbl = M.MK_LABEL[mk]
            desc = f"{pl} Anytime TD" if mk == "td" else f"{pl} {side} {line:g} {lbl}"
            act = f" → {actual:.0f}" if actual is not None else ""
            out.append(f"  {m} {desc} ({_fmt(price)}){act}")
    return "\n".join(out)


# ------------------------------------------------------------------ posting

_wh: dict[int, object] = {}


async def _post(bot, embed: discord.Embed | None = None, content: str | None = None) -> bool:
    ch = bot.get_channel(CHANNEL_ID)
    if not ch:
        log.warning("nflparlay: channel %d not found", CHANNEL_ID)
        return False
    wh = _wh.get(CHANNEL_ID)
    if wh is None:
        try:
            hooks = await ch.webhooks()
            wh = next((h for h in hooks if h.name == WEBHOOK_NAME), None) or await ch.create_webhook(name=WEBHOOK_NAME)
        except Exception:
            wh = False
        _wh[CHANNEL_ID] = wh
    try:
        if wh:
            await wh.send(content=content, embed=embed, username=DISPLAY_NAME)
        else:
            await ch.send(content=content, embed=embed)
        return True
    except Exception:
        log.exception("nflparlay post failed")
        return False


def _parse_day_time(spec: str, default=(6, 11, 45)) -> tuple[int, int, int]:
    try:
        d, hm = spec.split()
        h, m = (int(x) for x in hm.split(":"))
        return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(d[:3].lower()), h, m
    except Exception:
        return default


def _posted(week: int, kind: str, day: str | None = None) -> bool:
    with _conn() as c:
        if day:
            return c.execute("SELECT 1 FROM nfl_parlay_legs WHERE season=? AND week=? AND kind=? AND ticket LIKE ?",
                             (SEASON, week, kind, f"%-{day}")).fetchone() is not None
        return c.execute("SELECT 1 FROM nfl_parlay_legs WHERE season=? AND week=? AND kind=?",
                         (SEASON, week, kind)).fetchone() is not None


def _mark_skip(week: int, kind: str, day: str | None = None):
    ticket = f"{SEASON}-W{week}-{kind}-skip" + (f"-{day}" if day else "")
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO nfl_parlay_legs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (SEASON, week, kind, ticket, int(time.time()), "__none__", "no ticket", "", "pass", "", 0,
                   0, "", 0, 0, 0, None, "skip"))


async def post_kind(bot, kind: str, week: int, events: list[dict], day: str | None = None) -> bool:
    if kind == "prime" and len(events) == 1:
        kind = "sgp"
    week, book, legs, note = await asyncio.to_thread(build, kind, week, None, False, None, None, events)
    ok = await _post(bot, ticket_embed(kind, week, book, legs, dry=False, note=note))
    if ok and legs:
        t = store(kind, week, book, legs)
        if day:
            with _conn() as c:
                c.execute("UPDATE nfl_parlay_legs SET ticket=? WHERE ticket=?", (t + f"-{day}", t))
    elif ok:
        _mark_skip(week, kind, day)
    return ok


async def weekly_task(bot):
    await bot.wait_until_ready()
    try:
        await asyncio.to_thread(ratio_table)
    except Exception:
        log.exception("ratio table fit failed — parlays off until it succeeds")
    rd, rh, rm = _parse_day_time(RECAP_ET, (1, 10, 0))
    log.info("nflparlay v2: Sunday 4 tickets / other days 1 ticket, %dm before the first 11am-6pm ET kickoff; "
             "ONE evening ticket %dm before the first 6pm+ kickoff (SGP if one game), early games skipped, "
             "recap %s • %s", LEAD_MIN, PRIME_LEAD_MIN, RECAP_ET,
             f"-> channel {CHANNEL_ID}" if CHANNEL_ID else "OFF (no NFL_PARLAY_CHANNEL_ID; commands only)")
    last_recap = None
    while not bot.is_closed():
        now_dt = datetime.now(ET)
        now = int(time.time())
        try:
            if CHANNEL_ID and now_dt.weekday() == rd and (now_dt.hour, now_dt.minute) >= (rh, rm) \
                    and last_recap != now_dt.date():
                await asyncio.to_thread(grade)
                wk = await asyncio.to_thread(current_week)
                txt = record_text(week=wk - 1)
                if not txt.startswith("No legs"):
                    await _post(bot, content=f"**Week {wk - 1} recap**\n```\n{txt[:1800]}\n```")
                last_recap = now_dt.date()
            if CHANNEL_ID:
                day = now_dt.strftime("%a%d").lower()          # e.g. sun27 — dedupe per calendar day
                day_games = await asyncio.to_thread(day_slate, now, "day")
                if day_games and now >= min(e["kick"] for e in day_games) - LEAD_MIN * 60:
                    wk = await asyncio.to_thread(current_week)
                    kinds = ("general", "rush", "rec", "td") if now_dt.weekday() == 6 else ("general",)
                    for kind in kinds:
                        if not _posted(wk, kind, day):
                            await post_kind(bot, kind, wk, day_games, day)
                            await asyncio.sleep(3)
                eve = await asyncio.to_thread(day_slate, now, "evening")
                if eve and now >= min(e["kick"] for e in eve) - PRIME_LEAD_MIN * 60:
                    wk = await asyncio.to_thread(current_week)
                    if not _posted(wk, "prime", day) and not _posted(wk, "sgp", day):
                        await post_kind(bot, "prime", wk, eve, day)
        except Exception:
            log.exception("nflparlay weekly task failed")
        await asyncio.sleep(60)


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


KIND_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in KIND_LABEL.items() if k != "prime"]
DESC = {
    "nflparlay": "Build an NFL parlay now — pick the type (dry run unless post)",
    "nflparlay.kind": "Which ticket type to build",
    "nflparlay.game": "Optional: a team — restrict to that game (SGP uses the next kickoff otherwise)",
    "nflparlay.post": "Post to the parlay channel and log the legs (default: dry run)",
    "nflparlay.min_odds": "Optional: lowest American price per leg (e.g. -150 or 150)",
    "nflparlay.max_odds": "Optional: highest American price per leg (e.g. 400)",
    "nflrecord": "Forward log: every posted NFL parlay leg, graded, by ticket kind",
    "nflrecord.kind": "Optional: one ticket type",
}


def _chunks(text: str, n: int = 1900):
    while text:
        yield text[:n]
        text = text[n:]


def setup(bot):
    tree = bot.tree

    @tree.command(name="nflparlay", description=DESC["nflparlay"])
    @app_commands.describe(type=DESC["nflparlay.kind"], game=DESC["nflparlay.game"], post=DESC["nflparlay.post"],
                           min_odds=DESC["nflparlay.min_odds"], max_odds=DESC["nflparlay.max_odds"])
    @app_commands.choices(type=KIND_CHOICES)
    @_guarded
    async def nflparlay_cmd(interaction: discord.Interaction, type: app_commands.Choice[str],
                            game: str | None = None, post: bool = False,
                            min_odds: int | None = None, max_odds: int | None = None):
        await interaction.response.defer()
        kind = type
        week, book, legs, note = await asyncio.to_thread(build, kind.value, None, game, False, min_odds, max_odds)
        if post and not CHANNEL_ID:
            await interaction.followup.send("NFL_PARLAY_CHANNEL_ID isn't set — dry run instead.")
            post = False
        emb = ticket_embed(kind.value, week, book, legs, dry=not post, note=note)
        if post:
            ok = await _post(bot, emb)
            if ok and legs:
                store(kind.value, week, book, legs)
            await interaction.followup.send("Posted." if ok else "Post failed — check the log.")
        else:
            await interaction.followup.send(embed=emb)

    @tree.command(name="nflrecord", description=DESC["nflrecord"])
    @app_commands.describe(type=DESC["nflrecord.kind"])
    @app_commands.choices(type=KIND_CHOICES)
    @_guarded
    async def nflrecord_cmd(interaction: discord.Interaction, type: app_commands.Choice[str] | None = None):
        await interaction.response.defer()
        await asyncio.to_thread(grade)
        for ch in _chunks(record_text(type.value if type else None)):
            await interaction.followup.send(f"```\n{ch}\n```")

    log.info("nflparlay v2: registered /nflparlay /nflrecord")


def start(bot):
    if getattr(bot, "_nflparlay_task", None) is None:
        bot._nflparlay_task = bot.loop.create_task(weekly_task(bot))
