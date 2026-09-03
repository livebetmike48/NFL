"""
NFL ODDS API -- thin wrapper over The Odds API v4 for americanfootball_nfl.

Self-contained on purpose: this is a NEW repo/service, so it does not import
Bot Cooks' odds_api.py. Same key, separate service, separate credit meter.

Credit model (the-odds-api v4):
  /events              -> FREE (0 credits)
  /events/{id}/odds    -> markets x regions credits, PER EVENT

Env:
  ODDS_API_KEY   -- required (the NEW key from the Sept 2 subscription)
  ODDS_REGIONS   -- default "us"
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("nfl_odds")

BASE = "https://api.the-odds-api.com/v4"
SPORT = "americanfootball_nfl"
KEY = os.getenv("ODDS_API_KEY", "")
REGIONS = os.getenv("ODDS_REGIONS", "us")
TIMEOUT = 20

# Running total of credits this process has spent, for the poll receipts.
_spent = 0
_remaining: str | None = None


def credits_spent() -> int:
    return _spent


def credits_remaining() -> str | None:
    """Whatever the API last reported in x-requests-remaining."""
    return _remaining


def _get(path: str, params: dict) -> list | dict | None:
    global _remaining
    if not KEY:
        log.critical("ODDS_API_KEY is not set -- no odds will be fetched")
        return None
    params = dict(params or {})
    params["apiKey"] = KEY
    try:
        r = requests.get(f"{BASE}{path}", params=params, timeout=TIMEOUT)
    except Exception:
        log.exception("odds request failed: %s", path)
        return None
    rem = r.headers.get("x-requests-remaining")
    if rem is not None:
        _remaining = rem
    if r.status_code == 401:
        log.critical("Odds API 401 -- the key is dead or deactivated. A new "
                     "subscription issues a NEW key; update ODDS_API_KEY.")
        return None
    if r.status_code == 422:
        # Almost always an unsupported market for this sport -- log the body,
        # it names the offending key.
        log.error("Odds API 422 on %s: %s", path, r.text[:400])
        return None
    if r.status_code != 200:
        log.error("Odds API %d on %s: %s", r.status_code, path, r.text[:200])
        return None
    try:
        return r.json()
    except Exception:
        log.exception("odds response was not JSON: %s", path)
        return None


def get_events() -> list:
    """Upcoming NFL events. FREE call -- 0 credits."""
    data = _get(f"/sports/{SPORT}/events", {})
    return data if isinstance(data, list) else []


def get_event_props(event_id: str, markets: str) -> dict | None:
    """Player props for ONE event. Costs len(markets) x len(regions) credits."""
    global _spent
    if not event_id or not markets:
        return None
    data = _get(f"/sports/{SPORT}/events/{event_id}/odds",
                {"regions": REGIONS, "markets": markets,
                 "oddsFormat": "american"})
    if data is not None:
        _spent += len(markets.split(",")) * len(REGIONS.split(","))
    return data if isinstance(data, dict) else None
