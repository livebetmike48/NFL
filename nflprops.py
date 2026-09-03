"""
NFL PROPS TRACKER + OPENERS FEED -- the NFL bot's market half.

Ported from Bot Cooks props.py (MLB) with the NFL-specific changes:

  * Sport = americanfootball_nfl (nfl_odds.py, self-contained).
  * History keyed by EVENT, not calendar day. NFL props open Tuesday for a
    Sunday game; a day-keyed "open" would be Saturday's first quote. Here
    "open" = first quote ever seen for (game, market, player, book) and
    "now" = the latest one. Queries scope to the active slate (games that
    haven't finished yet).
  * Two-tier polling in ONE loop: ALERT markets (pass/rush/rec yards) every
    NFL_POLL_MIN (default 5); everything else every NFL_TRACK_POLL_MIN
    (default 30). Credits: 3 mkts x 16 games x 288 polls/day ~= 415K/mo
    + 10 mkts x 16 games x 48 polls/day ~= 230K/mo. Flat 13 @ 5-min would
    be ~1.8M/mo. The poll log prints real credit receipts every cycle.
  * Anytime TD is a Yes/No market (no line) -- stored with line=None,
    over=Yes price, under=No price, rendered as "Yes -145 / No +115".
  * Opener dedupe is (event_id, market, player) from day one -- once per
    game ever, restart-safe, no calendar rollover.

Openers post via a webhook displaying as "Openers" (needs Manage Webhooks
on the channel/category; falls back to a plain bot post if missing).
Everything else is command-only, no automated messages.

Commands (prefixed so they can't collide with Bot Cooks' /prop family):
  /nflprop <player> [market]   -- OPEN -> NOW per book, with movement
  /nflboard <market>           -- current slate board, latest per player/book
  /nflmoves [market]           -- biggest pregame movers (line vs price)

Env:
  NFL_PROPS_DB            -- sqlite path (VOLUME: /data/nflprops.db)
  NFL_POLL_MIN            -- alert-market poll cadence, default 5
  NFL_TRACK_POLL_MIN      -- track-only cadence, default 30
  NFL_LOOKAHEAD_DAYS      -- hard cap on how far ahead to poll, default 14
                             (the real window is ONE NFL WEEK: first upcoming
                             kickoff + 7 days -- see _week_window)
  NFL_BOOKS               -- default fanduel,draftkings,betmgm,williamhill_us
  NFL_OPENERS_CHANNEL_ID  -- catch-all openers channel
  NFL_OPENERS_PASS_ID / NFL_OPENERS_RUSH_ID / NFL_OPENERS_REC_ID
                          -- per-market channels (each falls back to catch-all)
  NFL_OPENERS_NAME        -- webhook display name, default "Openers"
  NFL_PROPS=0             -- kill switch
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

import nfl_odds

log = logging.getLogger("nflprops")

ET = ZoneInfo("America/New_York")
ENABLED = os.getenv("NFL_PROPS", "1") not in ("0", "false", "off")
POLL_MIN = max(1, int(os.getenv("NFL_POLL_MIN", "5") or 5))
TRACK_POLL_MIN = max(POLL_MIN, int(os.getenv("NFL_TRACK_POLL_MIN", "30") or 30))
LOOKAHEAD_DAYS = max(1, int(os.getenv("NFL_LOOKAHEAD_DAYS", "14") or 14))
# Opener->close history is the PRODUCT -- it must live on the volume.
DB = os.getenv("NFL_PROPS_DB") or (
    "/data/nflprops.db" if os.path.isdir("/data") else "nflprops.db")
BOOKS = [b.strip().lower() for b in os.getenv(
    "NFL_BOOKS", "fanduel,draftkings,betmgm,williamhill_us").split(",") if b.strip()]
BOOK_NAMES = {"fanduel": "FanDuel", "draftkings": "DraftKings",
              "betmgm": "BetMGM", "williamhill_us": "Caesars"}

# Games are treated as "active" (queryable slate) until this long after
# kickoff, so a Sunday 1pm board still reads right during the game.
GAME_WINDOW_SEC = 4 * 3600

# ------------------------------------------------------------------ markets
# Keys verified against The Odds API v4 docs (Sept 2026).
MARKETS = {
    # ALERT markets -- openers post when these first appear
    "player_pass_yds":             "Passing Yards",
    "player_rush_yds":             "Rushing Yards",
    "player_reception_yds":        "Receiving Yards",
    # TRACK-ONLY -- stored, command-queryable, silent
    "player_receptions":           "Receptions",
    "player_anytime_td":           "Anytime TD",
    "player_pass_tds":             "Passing TDs",
    "player_pass_completions":     "Completions",
    "player_pass_attempts":        "Pass Attempts",
    "player_rush_attempts":        "Rush Attempts",
    "player_pass_interceptions":   "Interceptions",
    "player_pass_longest_completion": "Longest Completion",
    "player_rush_longest":         "Longest Rush",
    "player_reception_longest":    "Longest Reception",
}
ALERT_MARKETS = {"player_pass_yds", "player_rush_yds", "player_reception_yds"}
TRACK_MARKETS = [m for m in MARKETS if m not in ALERT_MARKETS]
# Yes/No markets: no line, "over" = Yes price, "under" = No price.
YN_MARKETS = {"player_anytime_td"}

ALERT_PARAM = ",".join(m for m in MARKETS if m in ALERT_MARKETS)
TRACK_PARAM = ",".join(TRACK_MARKETS)
CHOICES = [app_commands.Choice(name=v, value=k) for k, v in MARKETS.items()]
assert len(CHOICES) <= 25, "Discord caps slash-command choices at 25"

OPENER_NAME = os.getenv("NFL_OPENERS_NAME", "Openers")
WEBHOOK_NAME = "LBM NFL Openers"


def _cid(var: str) -> int:
    return int(os.getenv(var, "0") or 0)


OPENERS_CHANNEL_ID = _cid("NFL_OPENERS_CHANNEL_ID")
OPENER_CHANNEL_BY_MARKET = {
    "player_pass_yds":      _cid("NFL_OPENERS_PASS_ID") or OPENERS_CHANNEL_ID,
    "player_rush_yds":      _cid("NFL_OPENERS_RUSH_ID") or OPENERS_CHANNEL_ID,
    "player_reception_yds": _cid("NFL_OPENERS_REC_ID") or OPENERS_CHANNEL_ID,
}


# ------------------------------------------------------------------ storage

def _conn():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS nfl_props_history (
        ts INTEGER, event_id TEXT, market TEXT, player TEXT,
        book TEXT, line REAL, over INTEGER, under INTEGER)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_nph ON nfl_props_history "
              "(event_id, market, player, book, ts)")
    c.execute("""CREATE TABLE IF NOT EXISTS nfl_open_alerts (
        event_id TEXT, market TEXT, player TEXT,
        PRIMARY KEY (event_id, market, player))""")
    c.execute("""CREATE TABLE IF NOT EXISTS nfl_events (
        event_id TEXT PRIMARY KEY, commence_ts INTEGER,
        home TEXT, away TEXT)""")
    return c


def _iso_ts(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(
            str(s).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _events_map() -> dict[str, tuple[int, str, str]]:
    """event_id -> (commence_ts, home, away) for everything on file."""
    with _conn() as c:
        return {e: (t, h, a) for e, t, h, a in c.execute(
            "SELECT event_id, commence_ts, home, away FROM nfl_events "
            "WHERE commence_ts IS NOT NULL").fetchall()}


def _week_window(kickoffs, now: int) -> tuple[int, int]:
    """(lo, hi) epoch bounds for 'the slate' = ONE NFL WEEK of games.
    lo = now - GAME_WINDOW_SEC (games in progress still count).
    hi = first upcoming kickoff + 7 days, capped at LOOKAHEAD_DAYS.
    A fixed N-day lookahead breaks in the preseason gap (Sept 3 -> Sunday
    Sept 13 is 10+ days, so a 9-day window polled only Thu/Fri) and in
    season it would drag next week's empty games in. Anchoring on the
    first upcoming game tracks exactly the games that have props up."""
    lo = now - GAME_WINDOW_SEC
    cap = now + LOOKAHEAD_DAYS * 86400
    upcoming = [t for t in kickoffs if t and t >= lo]
    if not upcoming:
        return lo, cap
    return lo, min(min(upcoming) + 7 * 86400, cap)


def active_event_ids(now: int | None = None) -> set[str]:
    """Games on the current slate: kickoff inside the week window."""
    now = now or int(time.time())
    evmap = _events_map()
    lo, hi = _week_window([t for t, _, _ in evmap.values()], now)
    return {e for e, (t, _, _) in evmap.items() if lo <= t < hi}


def _tlabel(ts: int) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%a %-I:%M%p").lower()


def _fmt(p) -> str:
    if p is None:
        return "—"
    return f"+{p}" if p > 0 else str(p)


def quote_str(market: str, line, over, under) -> str:
    """One book's quote, market-aware (Yes/No vs line + O/U)."""
    if market in YN_MARKETS:
        return f"Yes {_fmt(over)} / No {_fmt(under)}"
    return f"{line} (O {_fmt(over)}/U {_fmt(under)})"


# ------------------------------------------------------------------ polling

def extract_quotes(event_data: dict) -> dict[tuple[str, str, str], dict]:
    """(market, player, book) -> {line, over, under} from one event payload.
    Yes/No markets land with line=None, over=Yes, under=No."""
    out: dict[tuple[str, str, str], dict] = {}
    for bm in (event_data or {}).get("bookmakers", []):
        bk = (bm.get("key") or "").lower()
        if bk not in BOOKS:
            continue
        for mk in bm.get("markets", []):
            mkey = mk.get("key")
            if mkey not in MARKETS:
                continue
            per: dict[str, dict] = {}
            yn = mkey in YN_MARKETS
            for oc in mk.get("outcomes", []):
                name = oc.get("description") or ""
                side = (oc.get("name") or "").lower()
                if not name:
                    continue
                q = per.setdefault(name, {"line": None})
                if yn:
                    if side == "yes":
                        q["over"] = oc.get("price")
                    elif side == "no":
                        q["under"] = oc.get("price")
                elif "over" in side:
                    q["line"] = oc.get("point")
                    q["over"] = oc.get("price")
                elif "under" in side:
                    if q.get("line") is None:
                        q["line"] = oc.get("point")
                    q["under"] = oc.get("price")
            for player, q in per.items():
                ok = (q.get("over") is not None) if yn else (q.get("line") is not None)
                if ok:
                    out[(mkey, player, bk)] = q
    return out


def record_poll(quotes: dict[tuple[str, str, str], dict], event_id: str,
                ts: int | None = None) -> tuple[int, list[dict]]:
    """Snapshot changed quotes; return (rows_written, openings).
    An opening = first quote EVER for (game, market, player) in an ALERT
    market -- once per game, restart-safe, no calendar rollover."""
    ts = ts or int(time.time())
    wrote = 0
    opened_keys = []
    with _conn() as c:
        for (market, player, book), q in quotes.items():
            last = c.execute(
                "SELECT line, over, under FROM nfl_props_history WHERE "
                "event_id=? AND market=? AND player=? AND book=? "
                "ORDER BY ts DESC LIMIT 1",
                (event_id, market, player, book)).fetchone()
            cur = (q.get("line"), q.get("over"), q.get("under"))
            if last is None or tuple(last) != cur:
                c.execute("INSERT INTO nfl_props_history VALUES (?,?,?,?,?,?,?,?)",
                          (ts, event_id, market, player, book, *cur))
                wrote += 1
            if market in ALERT_MARKETS:
                seen = c.execute(
                    "SELECT 1 FROM nfl_open_alerts WHERE event_id=? AND "
                    "market=? AND player=?", (event_id, market, player)).fetchone()
                if not seen:
                    c.execute("INSERT OR IGNORE INTO nfl_open_alerts VALUES (?,?,?)",
                              (event_id, market, player))
                    opened_keys.append((market, player))
    openings = []
    for market, player in opened_keys:
        books = {b: q for (mk, p, b), q in quotes.items()
                 if mk == market and p == player}
        openings.append({"market": market, "player": player,
                         "books": books, "event_id": event_id})
    return wrote, openings


def opener_embeds(openings: list[dict],
                  events: dict | None = None) -> list[tuple[str, discord.Embed]]:
    """[(market, embed)] -- ONE embed per player per market, sent the poll
    it first appears, routed to that market's channel."""
    events = events if events is not None else _events_map()
    out = []
    for o in openings:
        mk = o["market"]
        e = discord.Embed(title=f"🟢 {MARKETS.get(mk, mk)} opener — {o['player']}",
                          color=0x2ecc71)
        lines = [f"{BOOK_NAMES.get(b, b)}: {quote_str(mk, q.get('line'), q.get('over'), q.get('under'))}"
                 for b, q in sorted(o["books"].items())]
        e.description = "\n".join(lines) or "—"
        ev = events.get(o.get("event_id"))
        game = f"{ev[2]} @ {ev[1]} • {_tlabel(ev[0])} ET" if ev else ""
        e.set_footer(text=("opening line" + (f" • {game}" if game else "")
                           + " • /nflprop for open → now"))
        out.append((mk, e))
    return out


_webhook_cache: dict[int, object] = {}


async def _send_as_openers(bot, channel_id: int, embeds: list[discord.Embed]):
    """Post via a webhook displaying as OPENER_NAME; plain bot post fallback."""
    ch = bot.get_channel(channel_id)
    if not ch:
        log.warning("openers channel %d not found", channel_id)
        return
    wh = _webhook_cache.get(channel_id)
    if wh is None:
        try:
            hooks = await ch.webhooks()
            wh = next((h for h in hooks if h.name == WEBHOOK_NAME), None)
            if wh is None:
                wh = await ch.create_webhook(name=WEBHOOK_NAME)
            _webhook_cache[channel_id] = wh
        except Exception:
            log.warning("no Manage Webhooks in channel %d — posting as the bot",
                        channel_id)
            _webhook_cache[channel_id] = False
            wh = False
    for e in embeds:
        try:
            if wh:
                await wh.send(embed=e, username=OPENER_NAME)
            else:
                await ch.send(embed=e)
        except Exception:
            log.exception("opener send failed")


def _store_events(events: list) -> None:
    try:
        with _conn() as c:
            for ev in events or []:
                cts = _iso_ts(ev.get("commence_time"))
                if ev.get("id") and cts:
                    c.execute("INSERT OR REPLACE INTO nfl_events VALUES (?,?,?,?)",
                              (ev["id"], cts, ev.get("home_team") or "",
                               ev.get("away_team") or ""))
    except Exception:
        log.exception("nfl: commence_time store failed")


async def poll_once(bot=None, include_track: bool = False) -> int:
    """One poll. Alert markets always; track-only markets when asked.
    Returns snapshots written. Logs a credit receipt."""
    now = int(time.time())
    events = await asyncio.to_thread(nfl_odds.get_events)  # free
    _store_events(events)
    lo, hi = _week_window([_iso_ts(ev.get("commence_time"))
                           for ev in (events or [])], now)
    events = [ev for ev in (events or [])
              if ev.get("id") and (_iso_ts(ev.get("commence_time")) or 0) >= lo
              and (_iso_ts(ev.get("commence_time")) or 0) < hi]
    param = ALERT_PARAM + ("," + TRACK_PARAM if include_track else "")
    n_mk = len(param.split(","))
    wrote = 0
    openings_all: list[dict] = []
    before = nfl_odds.credits_spent()
    for ev in events:
        data = await asyncio.to_thread(nfl_odds.get_event_props, ev["id"], param)
        if not data:
            continue
        w, opens = record_poll(extract_quotes(data), ev["id"], now)
        wrote += w
        openings_all.extend(opens)
    used = nfl_odds.credits_spent() - before
    log.info("nfl poll: %d game(s) thru %s x %d market(s) = ~%d credits, "
             "%d snapshots, %d opener(s)%s", len(events), _tlabel(hi), n_mk,
             used, wrote, len(openings_all),
             f", {nfl_odds.credits_remaining()} remaining"
             if nfl_odds.credits_remaining() else "")
    if openings_all and bot is not None:
        evmap = _events_map()
        by_ch: dict[int, list] = {}
        for mk, e in opener_embeds(openings_all, evmap):
            cid = OPENER_CHANNEL_BY_MARKET.get(mk, 0)
            if cid:
                by_ch.setdefault(cid, []).append(e)
        for cid, es in by_ch.items():
            await _send_as_openers(bot, cid, es)
    return wrote


async def poll_task(bot):
    if not ENABLED:
        log.info("NFL_PROPS=0 — NFL props tracker off")
        return
    await bot.wait_until_ready()
    try:
        with _conn() as c:
            snaps = c.execute("SELECT COUNT(*) FROM nfl_props_history").fetchone()[0]
            evs = c.execute("SELECT COUNT(*) FROM nfl_events").fetchone()[0]
            opens = c.execute("SELECT COUNT(*) FROM nfl_open_alerts").fetchone()[0]
        log.info("nfl props db: %s — %d snapshots, %d events, %d openers on file",
                 DB, snaps, evs, opens)
        if not DB.startswith("/data"):
            log.critical("nfl props db %s is NOT on the /data volume — history "
                         "will NOT survive a deploy. Attach the volume or set "
                         "NFL_PROPS_DB.", DB)
    except Exception:
        log.exception("nfl props db receipt failed")
    log.info("NFL props tracker: alert markets every %dm, track-only every %dm, "
             "window = one NFL week (cap %dd), books %s — openers feed %s",
             POLL_MIN, TRACK_POLL_MIN, LOOKAHEAD_DAYS, ",".join(BOOKS),
             (f"as '{OPENER_NAME}' -> " + ",".join(
                 f"{MARKETS[m]}:{c}" for m, c in OPENER_CHANNEL_BY_MARKET.items() if c))
             if any(OPENER_CHANNEL_BY_MARKET.values()) else "OFF (command-only)")
    every = max(1, TRACK_POLL_MIN // POLL_MIN)
    cycle = 0
    while not bot.is_closed():
        try:
            await poll_once(bot, include_track=(cycle % every == 0))
        except Exception:
            log.exception("nfl props poll failed")
        cycle += 1
        await asyncio.sleep(POLL_MIN * 60)


# ------------------------------------------------------------------ queries

def open_now(player: str | None, market: str | None = None,
             pregame_only: bool = False, now: int | None = None):
    """[(market, matched_player, book, open_row, now_row, event_id)] for the
    active slate. Rows are (ts, line, over, under). player=None = everyone.
    pregame_only drops snapshots at/after kickoff so in-game lines never
    rank as movement."""
    now = now or int(time.time())
    active = active_event_ids(now)
    if not active:
        return []
    evmap = _events_map()
    with _conn() as c:
        rows = c.execute(
            "SELECT market, player, book, ts, line, over, under, event_id "
            "FROM nfl_props_history ORDER BY ts").fetchall()
    firsts: dict[tuple, tuple] = {}
    lasts: dict[tuple, tuple] = {}
    for mk, p, b, ts, line, over, under, ev in rows:
        if ev not in active:
            continue
        if pregame_only:
            cts = evmap.get(ev, (None,))[0]
            if cts and ts >= cts:
                continue
        if player and player.lower() not in p.lower():
            continue
        if market and mk != market:
            continue
        k = (mk, p, b, ev)
        if k not in firsts:
            firsts[k] = (ts, line, over, under)
        lasts[k] = (ts, line, over, under)
    return [(k[0], k[1], k[2], firsts[k], lasts[k], k[3]) for k in firsts]


def board(market: str, now: int | None = None) -> dict[str, dict[str, tuple]]:
    """player -> book -> latest (ts, line, over, under) on the active slate."""
    active = active_event_ids(now)
    if not active:
        return {}
    marks = ",".join("?" * len(active))
    with _conn() as c:
        rows = c.execute(
            f"SELECT player, book, line, over, under, MAX(ts) FROM nfl_props_history "
            f"WHERE market=? AND event_id IN ({marks}) GROUP BY player, book",
            (market, *active)).fetchall()
    out: dict[str, dict[str, tuple]] = {}
    for p, b, line, over, under, ts in rows:
        out.setdefault(p, {})[b] = (ts, line, over, under)
    return out


def _imp(american) -> float | None:
    if american is None:
        return None
    a = float(american)
    return 100.0 / (a + 100.0) if a > 0 else -a / (-a + 100.0)


def movers(market: str | None = None, now: int | None = None):
    """Biggest PREGAME movers on the active slate.
    (line_moves, price_moves): line_moves = [(delta_line, mk, player, book,
    open_row, now_row)]; price_moves (same line, or Yes/No markets) =
    [(prob_pts, ...)] by Over/Yes implied-probability change.
    No averaging — every row is one real book."""
    line_moves, price_moves = [], []
    for mk, p, b, o, n, _ev in open_now(None, market, pregame_only=True, now=now):
        _, ol, oo, ou = o
        _, nl, no, nu = n
        if ol is not None and nl is not None and ol != nl:
            line_moves.append((abs(nl - ol), mk, p, b, o, n))
        elif oo is not None and no is not None and oo != no:
            i0, i1 = _imp(oo), _imp(no)
            if i0 is not None and i1 is not None:
                price_moves.append((abs(i1 - i0) * 100, mk, p, b, o, n))
    line_moves.sort(key=lambda x: -x[0])
    price_moves.sort(key=lambda x: -x[0])
    return line_moves, price_moves


# ------------------------------------------------------------------ commands

def _move_line(market: str, open_row, now_row) -> str:
    _, ol, oo, ou = open_row
    ts, nl, no, nu = now_row
    opened = quote_str(market, ol, oo, ou)
    if open_row[1:] == now_row[1:]:
        return f"open {opened} — unmoved"
    return (f"open {opened} → now {quote_str(market, nl, no, nu)} "
            f"as of {_tlabel(ts)}")


def _guarded(fn):
    """A crashed command must SAY SO (no infinite 'thinking…')."""
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


def _game_tag(ev_id: str, evmap: dict) -> str:
    ev = evmap.get(ev_id)
    return f" ({ev[2]} @ {ev[1]})" if ev else ""


# Discord: command + param descriptions must be <= 100 chars (one over
# nukes the whole sync batch -- the /weather lesson). Asserted in tests.
DESC = {
    "nflprop": "NFL prop open → now per book: one player, or a whole market",
    "nflprop.player": "Optional: player name (partial fine). Omit for the whole market",
    "nflprop.market": "Optional: one market",
    "nflboard": "NFL current slate board for one market",
    "nflboard.market": "Market",
    "nflmoves": "NFL biggest pregame movers — line moves and price moves",
    "nflmoves.market": "Optional: one market (default all)",
}


def setup(bot):
    tree = bot.tree

    @tree.command(name="nflprop", description=DESC["nflprop"])
    @app_commands.describe(player=DESC["nflprop.player"], market=DESC["nflprop.market"])
    @app_commands.choices(market=CHOICES)
    @_guarded
    async def nflprop_cmd(interaction: discord.Interaction,
                          player: str | None = None,
                          market: app_commands.Choice[str] | None = None):
        await interaction.response.defer()
        if not player and not market:
            await interaction.followup.send(
                "Give me a player, a market, or both — e.g. `/nflprop mahomes`, "
                "`/nflprop market:Rushing Yards`, `/nflprop jefferson market:Receptions`.")
            return
        rows = open_now(player, market.value if market else None)
        if not rows:
            who = f"“{player}”" if player else "anyone"
            await interaction.followup.send(
                f"No tracked quotes for {who} on the active slate"
                + (f" in {market.name}" if market else "") + ".")
            return
        evmap = _events_map()
        if player:
            by_mp: dict[tuple[str, str, str], list] = {}
            for mk, p, b, o, n, ev in rows:
                by_mp.setdefault((mk, p, ev), []).append((b, o, n))
            e = discord.Embed(title=f"NFL props — {rows[0][1]}", color=0x3498db)
            for (mk, p, ev), items in sorted(by_mp.items()):
                val = "\n".join(f"{BOOK_NAMES.get(b, b)}: {_move_line(mk, o, n)}"
                                for b, o, n in sorted(items))
                e.add_field(name=f"{MARKETS.get(mk, mk)}{_game_tag(ev, evmap)}",
                            value=val[:1024], inline=False)
            e.set_footer(text="open = first quote the tracker saw for this game")
            await interaction.followup.send(embed=e)
            return
        by_p: dict[str, list] = {}
        for mk, p, b, o, n, ev in rows:
            by_p.setdefault(p, []).append((mk, b, o, n))

        def _pscore(items):
            best = 0.0
            for mk, _, o, n in items:
                if mk in YN_MARKETS:
                    i0, i1 = _imp(o[2]), _imp(n[2])
                    if i0 is not None and i1 is not None:
                        best = max(best, abs(i1 - i0) * 100)
                else:
                    best = max(best, abs((n[1] or 0) - (o[1] or 0)))
            return best
        e = discord.Embed(title=f"{market.name} — open → now (active slate)",
                          color=0x3498db)
        for p in sorted(by_p, key=lambda x: -_pscore(by_p[x]))[:24]:
            moved = [(mk, b, o, n) for mk, b, o, n in sorted(by_p[p]) if o[1:] != n[1:]]
            still = len(by_p[p]) - len(moved)
            lines = [f"{BOOK_NAMES.get(b, b)}: {_move_line(mk, o, n)}"
                     for mk, b, o, n in moved]
            if still:
                mk0, _, _, n0 = by_p[p][0]
                lines.append(f"{still} book(s) unmoved at "
                             f"{quote_str(mk0, n0[1], n0[2], n0[3])}")
            e.add_field(name=p, value="\n".join(lines)[:1024], inline=False)
        e.set_footer(text="sorted by biggest move • open = first quote seen for the game")
        await interaction.followup.send(embed=e)

    @tree.command(name="nflboard", description=DESC["nflboard"])
    @app_commands.describe(market=DESC["nflboard.market"])
    @app_commands.choices(market=CHOICES)
    @_guarded
    async def nflboard_cmd(interaction: discord.Interaction,
                           market: app_commands.Choice[str]):
        await interaction.response.defer()
        b = board(market.value)
        if not b:
            await interaction.followup.send(
                f"Nothing tracked yet for {market.name} on the active slate.")
            return
        e = discord.Embed(title=f"{market.name} — current board", color=0x9b59b6)
        for p in sorted(b)[:24]:
            val = "\n".join(
                f"{BOOK_NAMES.get(bk, bk)}: {quote_str(market.value, l, o, u)}"
                for bk, (ts, l, o, u) in sorted(b[p].items()))
            e.add_field(name=p, value=val[:1024], inline=False)
        e.set_footer(text=f"{len(b)} player(s) • latest quote per book")
        await interaction.followup.send(embed=e)

    @tree.command(name="nflmoves", description=DESC["nflmoves"])
    @app_commands.describe(market=DESC["nflmoves.market"])
    @app_commands.choices(market=CHOICES)
    @_guarded
    async def nflmoves_cmd(interaction: discord.Interaction,
                           market: app_commands.Choice[str] | None = None):
        await interaction.response.defer()
        lm, pm = movers(market.value if market else None)
        if not lm and not pm:
            await interaction.followup.send(
                "No pregame movement on the active slate yet"
                + (f" in {market.name}" if market else "") + ".")
            return
        e = discord.Embed(title=("NFL movers — " + (market.name if market else "all markets")),
                          color=0xe67e22)
        if lm:
            e.add_field(name="📏 Line moves", value="\n".join(
                f"**{p}** {MARKETS.get(mk, mk)} · {BOOK_NAMES.get(b, b)}: "
                f"{o[1]} → {n[1]} ({'+' if n[1] > o[1] else ''}{round(n[1] - o[1], 1)})"
                for _, mk, p, b, o, n in lm[:12])[:1024], inline=False)
        if pm:
            e.add_field(name="💵 Price moves (same line)", value="\n".join(
                f"**{p}** {MARKETS.get(mk, mk)} · {BOOK_NAMES.get(b, b)}: "
                f"{_fmt(o[2])} → {_fmt(n[2])} ({pts:.1f} pts)"
                for pts, mk, p, b, o, n in pm[:12])[:1024], inline=False)
        e.set_footer(text="pregame only • pts = implied-probability points • one row = one real book")
        await interaction.followup.send(embed=e)

    log.info("nflprops: registered /nflprop /nflboard /nflmoves")


def start(bot):
    """Arm the background poller. Call from on_ready (idempotent)."""
    if getattr(bot, "_nflprops_task", None) is None:
        bot._nflprops_task = bot.loop.create_task(poll_task(bot))
