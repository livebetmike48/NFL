import os, sys, time, tempfile, asyncio
os.environ["NFL_PROPS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["NFL_OPENERS_CHANNEL_ID"] = "111"
os.environ["NFL_OPENERS_RUSH_ID"] = "222"
import nflprops as P
import nfl_odds

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond: fails.append(name)

NOW = int(time.time())
TUE = NOW - 4*86400          # props opened Tuesday
KICK = NOW + 2*86400         # game Sunday
def ev(eid, cts, home="Kansas City Chiefs", away="Baltimore Ravens"):
    from datetime import datetime, timezone
    return {"id": eid, "commence_time": datetime.fromtimestamp(cts, timezone.utc).isoformat().replace("+00:00","Z"),
            "home_team": home, "away_team": away}
P._store_events([ev("E1", KICK), ev("E_far", NOW + 20*86400), ev("E_done", NOW - 5*3600)])

def payload(pass_line=275.5, over=-115, under=-105, td_yes=-150, td_no=120, books=("fanduel","draftkings")):
    bms = []
    for bk in books:
        bms.append({"key": bk, "markets": [
            {"key": "player_pass_yds", "outcomes": [
                {"name": "Over", "description": "Patrick Mahomes", "point": pass_line, "price": over},
                {"name": "Under", "description": "Patrick Mahomes", "point": pass_line, "price": under}]},
            {"key": "player_anytime_td", "outcomes": [
                {"name": "Yes", "description": "Isiah Pacheco", "price": td_yes},
                {"name": "No", "description": "Isiah Pacheco", "price": td_no}]},
            {"key": "player_receptions", "outcomes": [
                {"name": "Over", "description": "Travis Kelce", "point": 5.5, "price": -120},
                {"name": "Under", "description": "Travis Kelce", "point": 5.5, "price": -110}]},
            {"key": "player_points", "outcomes": [{"name":"Over","description":"X","point":1,"price":1}]},
        ]})
    return {"bookmakers": bms}

# 1. Discord limits
check("all command/param descriptions <= 100", all(len(v) <= 100 for v in P.DESC.values()),
      str(max(len(v) for v in P.DESC.values())))
check("choices <= 25", len(P.CHOICES) <= 25, str(len(P.CHOICES)))

# 2. extract
q = P.extract_quotes(payload())
check("O/U parsed", q[("player_pass_yds","Patrick Mahomes","fanduel")] == {"line":275.5,"over":-115,"under":-105})
check("Yes/No parsed line=None", q[("player_anytime_td","Isiah Pacheco","draftkings")] == {"line":None,"over":-150,"under":120})
check("unknown market ignored", not any(k[0]=="player_points" for k in q))
check("6 quotes (3 mkts x 2 books)", len(q) == 6, str(len(q)))

# 3. record_poll: first = openers only for alert markets
w, opens = P.record_poll(q, "E1", TUE)
check("first poll writes 6", w == 6, str(w))
check("opener only for pass yds (alert), not TD/receptions", [o["market"] for o in opens] == ["player_pass_yds"], str([o["market"] for o in opens]))
check("opener carries both books", set(opens[0]["books"]) == {"fanduel","draftkings"})
# identical repoll
w, opens = P.record_poll(q, "E1", TUE + 300)
check("unchanged repoll writes 0, opens 0", w == 0 and opens == [])
# line move Saturday
q2 = P.extract_quotes(payload(pass_line=280.5, over=-110, under=-110, td_yes=-165))
w, opens = P.record_poll(q2, "E1", NOW - 3600)
check("changed quotes write, NO re-open (dedupe by event)", w == 4 and opens == [], f"w={w} opens={len(opens)}")
# restart = new process, same DB -> still no re-open
w, opens = P.record_poll(q2, "E1", NOW - 1800)
check("restart-safe: no re-open after 'restart'", opens == [])

# 4. event-keyed open->now: open must be TUESDAY's quote
rows = P.open_now("mahomes", "player_pass_yds", now=NOW)
fd = next(r for r in rows if r[2]=="fanduel")
check("open = Tuesday line 275.5 (not today's)", fd[3][1] == 275.5 and fd[3][0] == TUE, str(fd[3]))
check("now = latest 280.5", fd[4][1] == 280.5)
check("row carries event_id", fd[5] == "E1")

# 5. active window
act = P.active_event_ids(NOW)
check("active = E1 only (far future + finished excluded)", act == {"E1"}, str(act))

# 6. movers
lm, pm = P.movers(now=NOW)
check("line move ranked: pass yds +5.0", lm and lm[0][0] == 5.0 and lm[0][1]=="player_pass_yds", str(lm[:1]))
check("YN price move ranked (TD -150 -> -165)", any(m[1]=="player_anytime_td" for m in pm), str([m[1] for m in pm]))
check("no line-move for YN market", not any(m[1]=="player_anytime_td" for m in lm))
# in-game snapshot excluded
q3 = P.extract_quotes(payload(pass_line=300.5))
P.record_poll(q3, "E1", KICK + 600)
lm2, _ = P.movers(now=KICK + 700)
check("pregame_only: in-game 300.5 never ranks", lm2 and lm2[0][0] == 5.0, str(lm2[:1]))

# 7. board
b = P.board("player_anytime_td", now=NOW)
check("board YN latest (in-game snapshot at KICK+600 is newest) = -150", b["Isiah Pacheco"]["fanduel"][2] == -150, str(b["Isiah Pacheco"]["fanduel"]))
check("quote_str YN", P.quote_str("player_anytime_td", None, -165, 130) == "Yes -165 / No +130")
check("quote_str OU", P.quote_str("player_pass_yds", 275.5, -115, -105) == "275.5 (O -115/U -105)")

# 8. embeds + routing
_, opens = P.record_poll(P.extract_quotes(payload()), "E2", NOW)   # E2 not stored -> no footer game
P._store_events([ev("E3", KICK, "Buffalo Bills", "Miami Dolphins")])
_, opens3 = P.record_poll(P.extract_quotes(payload()), "E3", NOW)
embs = P.opener_embeds(opens3)
check("one embed per player per alert market", len(embs) == 1 and embs[0][0] == "player_pass_yds")
check("embed title", embs[0][1].title == "🟢 Passing Yards opener — Patrick Mahomes", embs[0][1].title)
check("embed footer names the game", "Miami Dolphins @ Buffalo Bills" in embs[0][1].footer.text, embs[0][1].footer.text)
check("routing: pass -> catch-all 111, rush -> 222", P.OPENER_CHANNEL_BY_MARKET["player_pass_yds"]==111 and P.OPENER_CHANNEL_BY_MARKET["player_rush_yds"]==222)

# 9. two-tier cadence
check("5/30 -> track every 6th cycle", max(1, P.TRACK_POLL_MIN // P.POLL_MIN) == 6)
check("alert param = 3 markets", P.ALERT_PARAM.count(",") == 2)
check("track param = 10 markets", P.TRACK_PARAM.count(",") == 9)

# 10. credit accounting + 401 handling (mock requests)
class R:
    def __init__(s, code, js=None, hdr=None): s.status_code=code; s._js=js; s.headers=hdr or {}; s.text="x"
    def json(s): return s._js
calls = []
def fake_get(url, params=None, timeout=None):
    calls.append(url)
    if url.endswith("/events"): return R(200, [ev("E1", KICK)], {"x-requests-remaining":"4900000"})
    if "/events/E1/odds" in url: return R(200, payload(), {"x-requests-remaining":"4899987"})
    return R(401)
nfl_odds.requests.get = fake_get
nfl_odds.KEY = "k"
b0 = nfl_odds.credits_spent()
nfl_odds.get_events()
check("/events costs 0", nfl_odds.credits_spent() == b0)
nfl_odds.get_event_props("E1", P.ALERT_PARAM + "," + P.TRACK_PARAM)
check("event props = 13 credits (13 mkts x 1 region)", nfl_odds.credits_spent() - b0 == 13, str(nfl_odds.credits_spent()-b0))
check("remaining header captured", nfl_odds.credits_remaining() == "4899987")
check("401 returns None", nfl_odds.get_event_props("dead", "player_pass_yds") is None)

# 11. poll_once end-to-end with mocked API (no bot)
os.environ["NFL_PROPS_DB"] = os.path.join(tempfile.mkdtemp(), "t2.db"); P.DB = os.environ["NFL_PROPS_DB"]
n = asyncio.run(P.poll_once(None, include_track=True))
check("poll_once end-to-end stores snapshots", n == 6, str(n))
n = asyncio.run(P.poll_once(None, include_track=False))
check("alert-only repoll unchanged -> 0 writes", n == 0, str(n))

# 12. command registration
import discord
class Dummy(discord.Client):
    def __init__(s): super().__init__(intents=discord.Intents.default()); s.tree = discord.app_commands.CommandTree(s)
d = Dummy(); P.setup(d)
names = sorted(c.name for c in d.tree.get_commands())
check("commands registered", names == ["nflboard","nflmoves","nflprop"], str(names))
for c in d.tree.get_commands():
    check(f"/{c.name} desc <=100", len(c.description) <= 100)
    for p in c.parameters:
        check(f"/{c.name} {p.name} desc <=100", len(p.description) <= 100)


# 13. week window -- the live bug: Sept 3, Thu 9/10 + Fri 9/11 + Sun 9/13 + Mon 9/14 + Thu 9/17
from datetime import datetime, timezone
def T(s): return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())
now = T("2026-09-03T03:28:00")
ks = {"thu": T("2026-09-11T00:15:00"), "fri": T("2026-09-12T00:00:00"), "sun": T("2026-09-13T17:00:00"),
      "mon": T("2026-09-15T00:15:00"), "thu2": T("2026-09-18T00:15:00")}
lo, hi = P._week_window(list(ks.values()), now)
inwin = {k for k, t in ks.items() if lo <= t < hi}
check("Sept 3 window covers Thu/Fri/SUN/MON, excludes next Thu", inwin == {"thu","fri","sun","mon"}, str(inwin))
# in-season Tuesday
now2 = T("2026-09-15T14:00:00")
ks2 = {"thu2": ks["thu2"], "sun2": T("2026-09-20T17:00:00"), "mon2": T("2026-09-22T00:15:00"), "thu3": T("2026-09-25T00:15:00")}
lo, hi = P._week_window(list(ks2.values()), now2)
check("in-season Tue: exactly next week's games", {k for k,t in ks2.items() if lo<=t<hi} == {"thu2","sun2","mon2"})
# during MNF: game in progress still on the slate
now3 = ks["mon"] + 3600
lo, hi = P._week_window(list(ks.values()) + list(ks2.values()), now3)
check("MNF in progress stays on slate", lo <= ks["mon"] < hi)
check("no events -> falls back to cap", P._week_window([], now) == (now - P.GAME_WINDOW_SEC, now + P.LOOKAHEAD_DAYS*86400))
print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + str(fails)}")
sys.exit(1 if fails else 0)
