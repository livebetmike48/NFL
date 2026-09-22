"""
LBM NFL BOT -- entry point.

Modules:
  nflprops  -- props tracker + openers webhook (Phase 1)
  nflstats  -- nflverse stats / usage / defense / injuries / depth / roster (Phase 2)

Env:
  DISCORD_TOKEN  -- required
  ODDS_API_KEY   -- required (see nfl_odds.py)
  see nflprops.py / nfl_data.py for the tracker + data vars
"""
import logging
import os

import discord
from dotenv import load_dotenv

import nflprops
import nflparlay
import nflstats

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("nflbot")

TOKEN = os.getenv("DISCORD_TOKEN", "")

intents = discord.Intents.default()


class NFLBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)
        self._synced = False

    async def setup_hook(self):
        nflprops.setup(self)
        nflstats.setup(self)
        nflparlay.setup(self)
        cmds = await self.tree.sync()
        log.info("synced %d command(s): %s", len(cmds),
                 ", ".join(f"/{c.name}" for c in cmds))

    async def on_ready(self):
        # Reconnect guard: on_ready fires again after a resume.
        log.info("Logged in as %s", self.user)
        nflprops.start(self)
        nflstats.start(self)
        nflparlay.start(self)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set")
    NFLBot().run(TOKEN, log_handler=None)
