import asyncio
import logging
import os
import threading
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask, redirect, send_file

from database import Database
from updates import announce_update

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("werkzeug").setLevel(logging.WARNING)
log = logging.getLogger("giftly")

TOKEN = os.environ.get("DISCORD_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
missing = [n for n, v in (("DISCORD_TOKEN", TOKEN), ("DATABASE_URL", DATABASE_URL)) if not v]
if missing:
    raise SystemExit(f"Missing environment variable(s): {', '.join(missing)}. Add them in Render > Environment.")

HERE = Path(__file__).parent
# View Channel + Send Messages + Embed Links + Read Message History
INVITE_PERMISSIONS = 84992

# ---------------------------------------------------------------- website
web = Flask(__name__)


def invite_url():
    client_id = os.environ.get("DISCORD_CLIENT_ID") or bot.application_id or (bot.user.id if bot.user else None)
    if not client_id:
        return None
    return ("https://discord.com/oauth2/authorize"
            f"?client_id={client_id}&permissions={INVITE_PERMISSIONS}&scope=bot%20applications.commands")


@web.route("/")
def home():
    page = HERE / "site.html"
    html = page.read_text(encoding="utf-8") if page.exists() else "<h1>Giftly</h1><a href='{{INVITE_URL}}'>Add to Discord</a>"
    return html.replace("{{INVITE_URL}}", invite_url() or "#")


@web.route("/terms")
def terms():
    page = HERE / "terms.html"
    return page.read_text(encoding="utf-8") if page.exists() else ("Terms not found.", 404)


@web.route("/invite")
def invite():
    url = invite_url()
    return redirect(url) if url else ("Giftly is starting up. Try again in a few seconds.", 503)


@web.route("/logo.png")
def logo():
    path = HERE / "logo.png"
    return send_file(path, mimetype="image/png") if path.exists() else ("", 404)


@web.route("/health")
def health():
    return "ok", 200


def run_web():
    web.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))


# ---------------------------------------------------------------- bot
class Giftly(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),  # no privileged intents needed
            help_command=None,
        )
        self.db = Database(DATABASE_URL)
        self._update_checked = False
        self._background = set()

    async def setup_hook(self):
        try:
            await self.db.connect()
        except Exception:
            log.exception("Could not connect to the database. Check DATABASE_URL.")
            raise
        await self.load_extension("giveaways")
        self.tree.on_error = self.on_tree_error
        await self.tree.sync()
        self.keep_awake.start()

    @tasks.loop(minutes=10)
    async def keep_awake(self):
        """Ping our own public URL so Render's free tier doesn't spin the service down."""
        url = os.environ.get("RENDER_EXTERNAL_URL")
        if not url:
            return
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{url}/health") as resp:
                    log.debug("keep-alive ping: %s", resp.status)
        except Exception as exc:
            log.warning("keep-alive ping failed: %s", exc)

    @keep_awake.before_loop
    async def _before_keep_awake(self):
        await asyncio.sleep(45)  # give the web server a moment to start

    async def on_ready(self):
        log.info("Logged in as %s (%s servers)", self.user, len(self.guilds))
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="giveaways"))
        if not self._update_checked:  # on_ready can fire again after a reconnect
            self._update_checked = True
            task = asyncio.create_task(announce_update(self))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            text = str(error) or "You can't use that command."
        else:
            log.error("Command error", exc_info=error)
            text = "Something went wrong on my end. Please try again in a moment."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


bot = Giftly()


@bot.tree.command(name="ping", description="Check that Giftly is online")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong. {round(bot.latency * 1000)}ms", ephemeral=True)


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    bot.run(TOKEN, log_handler=None)
