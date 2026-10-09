import os
import threading

import discord
from discord import app_commands
from flask import Flask

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("DISCORD_TOKEN env var is missing. Set it in Render > Environment.")

# ---------- Keep-alive web server (Render needs an open port) ----------
web = Flask(__name__)


@web.route("/")
def home():
    return "Bot is running", 200


def run_web():
    web.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))


# ---------- Ticket buttons (persistent across restarts) ----------
class CloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red, custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Closing ticket...")
        await interaction.channel.delete()


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.green, custom_id="ticket:open")
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        channel = await guild.create_text_channel(f"ticket-{interaction.user.name}", overwrites=overwrites)
        await channel.send(f"{interaction.user.mention} staff will be with you shortly.", view=CloseView())
        await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)


# ---------- Bot ----------
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(TicketView())
        self.add_view(CloseView())
        await self.tree.sync()


bot = Bot()


@bot.tree.command(name="ping", description="Check if the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)}ms")


@bot.tree.command(name="panel", description="Post the ticket panel")
@app_commands.default_permissions(administrator=True)
async def panel(interaction: discord.Interaction):
    embed = discord.Embed(title="Support", description="Click below to open a ticket.", color=0x5865F2)
    await interaction.channel.send(embed=embed, view=TicketView())
    await interaction.response.send_message("Panel posted.", ephemeral=True)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    bot.run(TOKEN)
