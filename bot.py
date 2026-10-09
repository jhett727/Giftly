import logging
import os
import threading
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask, redirect, send_file

from database import Database

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

    async def setup_hook(self):
        try:
            await self.db.connect()
        except Exception:
            log.exception("Could not connect to the database. Check DATABASE_URL.")
            raise
        await self.load_extension("giveaways")
        self.tree.on_error = self.on_tree_error
        await self.tree.sync()

    async def on_ready(self):
        log.info("Logged in as %s (%s servers)", self.user, len(self.guilds))
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="giveaways"))

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
