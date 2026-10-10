"""Everything that helps Giftly itself reach more servers: help and invite commands,
the welcome message, join/leave tracking, live status, and top.gg stats and votes."""
import datetime as dt
import logging
import os

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import DEV_GUILD_ID, build_invite_url, public_url

log = logging.getLogger("giftly.growth")

BRAND = "Dormexed Productions"
SUPPORT_URL = os.environ.get("SUPPORT_URL", "https://discord.gg/64tyAkaF9g")
VIOLET = 0x7C3AED


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def span(seconds):
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    parts = [f"{days}d" if days else "", f"{hours}h" if hours or days else "", f"{rest // 60}m"]
    return " ".join(p for p in parts if p)


def plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def link_view(links):
    view = discord.ui.View(timeout=None)
    for label, url in links:
        if url:
            view.add_item(discord.ui.Button(label=label, url=url))
    return view


class Growth(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.started = utcnow()
        self.phase = 0
        self.topgg_token = os.environ.get("TOPGG_TOKEN")
        self.log_webhook = os.environ.get("LOG_WEBHOOK_URL")
        self.log_channel_id = int(os.environ.get("LOG_CHANNEL_ID") or 0)

    async def cog_load(self):
        self.status_loop.start()
        self.topgg_loop.start()

    async def cog_unload(self):
        self.status_loop.cancel()
        self.topgg_loop.cancel()

    # ------------------------------------------------------------------ links
    @property
    def client_id(self):
        return self.bot.application_id or (self.bot.user.id if self.bot.user else None)

    def invite(self):
        return build_invite_url(self.client_id) if self.client_id else None

    def vote_url(self):
        if not self.topgg_token or not self.client_id:
            return None
        return os.environ.get("TOPGG_URL") or f"https://top.gg/bot/{self.client_id}/vote"

    def links(self, vote=True):
        site = public_url() or None
        out = [("Add to your server", self.invite()), ("Support server", SUPPORT_URL), ("Website", site)]
        if vote and self.vote_url():
            out.append(("Vote", self.vote_url()))
        return out

    async def reply(self, interaction, embed, links, ephemeral=False):
        view = link_view(links)
        options = {"embed": embed, "ephemeral": ephemeral}
        if view.children:
            options["view"] = view
        await interaction.response.send_message(**options)

    # ------------------------------------------------------------------ status + stats
    @tasks.loop(minutes=5)
    async def status_loop(self):
        count = len(self.bot.guilds)
        lines = [plural(count, "server"), "giveaways | /help", BRAND]
        name = lines[self.phase % len(lines)]
        self.phase += 1
        await self.bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=name))

    @status_loop.before_loop
    async def _before_status(self):
        await self.bot.wait_until_ready()
        try:
            await self.db.sync_guilds([(g.id, g.name, g.member_count or 0) for g in self.bot.guilds])
        except Exception:
            log.exception("Could not sync the server list")

    @tasks.loop(minutes=30)
    async def topgg_loop(self):
        if not self.topgg_token or not self.client_id:
            return
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"https://top.gg/api/bots/{self.client_id}/stats",
                                        json={"server_count": len(self.bot.guilds)},
                                        headers={"Authorization": self.topgg_token}) as resp:
                    if resp.status != 200:
                        log.warning("top.gg stats post returned %s", resp.status)
        except Exception as exc:
            log.warning("top.gg stats post failed: %s", exc)

    @topgg_loop.before_loop
    async def _before_topgg(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ logging
    async def send_log(self, embed):
        try:
            if self.log_webhook:
                async with aiohttp.ClientSession() as session:
                    await discord.Webhook.from_url(self.log_webhook, session=session).send(
                        embed=embed, username="Giftly")
            elif self.log_channel_id:
                channel = self.bot.get_channel(self.log_channel_id) or await self.bot.fetch_channel(self.log_channel_id)
                await channel.send(embed=embed)
        except Exception as exc:
            log.warning("Could not post to the log channel: %s", exc)

    async def server_log(self, kind, guild):
        embed = discord.Embed(title=f"Giftly {kind} a server",
                              color=0x22C55E if kind == "joined" else 0xEF4444, timestamp=utcnow())
        embed.add_field(name="Server", value=f"{guild.name}\n`{guild.id}`")
        embed.add_field(name="Members", value=f"{guild.member_count or 0:,}")
        embed.add_field(name="Total servers", value=f"{len(self.bot.guilds):,}")
        await self.send_log(embed)

    # ------------------------------------------------------------------ joining and leaving
    @staticmethod
    def pick_channel(guild):
        for channel in [guild.system_channel, *guild.text_channels]:
            if channel is None:
                continue
            perms = channel.permissions_for(guild.me)
            if perms.view_channel and perms.send_messages and perms.embed_links:
                return channel
        return None

    async def send_welcome(self, guild):
        channel = self.pick_channel(guild)
        if channel is None:
            return
        embed = discord.Embed(
            title="Thanks for adding Giftly",
            description="Giftly runs giveaways in your server. Here's the quick way to get going.",
            color=VIOLET)
        embed.add_field(name="1. Set it up",
                        value="Run `/giveaway settings` to pick your color, roles and messages. You need the Manage Server permission.",
                        inline=False)
        embed.add_field(name="2. Start one",
                        value="Run `/giveaway start`, choose a prize and how long it runs, and Giftly does the rest.",
                        inline=False)
        embed.add_field(name="3. Stuck?",
                        value="`/help` lists every command, and the support server is one tap away.",
                        inline=False)
        embed.set_footer(text=f"Giftly by {BRAND}")
        try:
            await channel.send(embed=embed, view=link_view(self.links(vote=False)[1:]))
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        try:
            await self.db.guild_joined(guild.id, guild.name, guild.member_count or 0)
        except Exception:
            log.exception("Could not record joining %s", guild.id)
        await self.send_welcome(guild)
        await self.server_log("joined", guild)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        try:
            await self.db.guild_left(guild.id)
        except Exception:
            log.exception("Could not record leaving %s", guild.id)
        await self.server_log("left", guild)

    # ------------------------------------------------------------------ votes (called by the web server)
    async def handle_vote(self, user_id, test=False):
        embed = discord.Embed(color=VIOLET, timestamp=utcnow())
        if test:
            embed.title = "Vote webhook test received"
            embed.description = "The top.gg connection works."
            return await self.send_log(embed)
        total = await self.db.record_vote(user_id)
        embed.title = "New vote on top.gg"
        embed.description = f"<@{user_id}> voted. That's their vote number {total}."
        await self.send_log(embed)
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            thanks = discord.Embed(
                title="Thanks for voting",
                description=f"Your vote helps more servers find Giftly. You've voted {plural(total, 'time')}. "
                            "You can vote again in 12 hours.",
                color=VIOLET)
            view = link_view(self.links())
            await user.send(embed=thanks, **({"view": view} if view.children else {}))
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------ commands
    @app_commands.command(name="help", description="See what Giftly can do")
    async def help_cmd(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Giftly",
            description="Giveaways for your server, with winner claims, bonus entries and settings you control.",
            color=VIOLET)
        embed.add_field(
            name="Run giveaways",
            value="`/giveaway start` create one\n`/giveaway end` finish early\n`/giveaway reroll` draw again\n"
                  "`/giveaway edit` change a running one\n`/giveaway cancel` stop one\n"
                  "`/giveaway list` see what's running\n`/giveaway info` details of one",
            inline=False)
        embed.add_field(
            name="Set it up",
            value="`/giveaway settings` roles, look, messages and defaults\n"
                  "`/giveaway bonus-add` extra entries for a role",
            inline=False)
        extras = "`/invite` `/support` `/about`" + (" `/vote`" if self.vote_url() else "")
        embed.add_field(name="About Giftly", value=extras, inline=False)
        embed.set_footer(text=f"Powered by {BRAND}")
        await self.reply(interaction, embed, self.links(), ephemeral=True)

    @app_commands.command(name="invite", description="Add Giftly to your server")
    async def invite_cmd(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Add Giftly to your server",
                              description="Giveaways with claims, bonus entries and full settings. Setup takes a minute.",
                              color=VIOLET)
        await self.reply(interaction, embed, self.links(vote=False))

    @app_commands.command(name="support", description="Get help from the Giftly support server")
    async def support_cmd(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Need a hand?",
                              description="Questions, bugs and ideas all go in the support server.", color=VIOLET)
        await self.reply(interaction, embed, [("Join the support server", SUPPORT_URL)])

    @app_commands.command(name="about", description="About Giftly")
    async def about_cmd(self, interaction: discord.Interaction):
        members = sum(g.member_count or 0 for g in self.bot.guilds)
        version = (os.environ.get("RENDER_GIT_COMMIT") or "dev")[:7]
        embed = discord.Embed(title="About Giftly", color=VIOLET,
                              description=f"A giveaway bot made by {BRAND}.")
        embed.add_field(name="Servers", value=f"{len(self.bot.guilds):,}")
        embed.add_field(name="Members", value=f"{members:,}")
        embed.add_field(name="Ping", value=f"{round(self.bot.latency * 1000)}ms")
        embed.add_field(name="Uptime", value=span((utcnow() - self.started).total_seconds()))
        embed.add_field(name="Version", value=f"`{version}`")
        embed.add_field(name="Library", value=f"discord.py {discord.__version__}")
        await self.reply(interaction, embed, self.links())

    async def send_vote(self, interaction):
        row = await self.db.vote_row(interaction.user.id)
        text = "Voting takes a few seconds and helps other servers find Giftly. You can vote every 12 hours."
        if row:
            text += f"\nYou've voted {plural(row['total'], 'time')} so far. Thank you."
        embed = discord.Embed(title="Vote for Giftly", description=text, color=VIOLET)
        await self.reply(interaction, embed, [("Vote on top.gg", self.vote_url())], ephemeral=True)

    @app_commands.command(name="stats", description="Giftly growth numbers (bot owner only)")
    @app_commands.guilds(discord.Object(id=DEV_GUILD_ID))
    async def stats_cmd(self, interaction: discord.Interaction):
        if not await self.bot.is_owner(interaction.user):
            return await interaction.response.send_message("Only the bot owner can use that.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        guilds = await self.db.guild_stats()
        totals = await self.db.giveaway_totals()
        votes = await self.db.vote_totals()
        top = await self.db.top_guilds(5)
        embed = discord.Embed(title="Giftly stats", color=VIOLET, timestamp=utcnow())
        embed.add_field(name="Servers", value=f"{len(self.bot.guilds):,}")
        embed.add_field(name="Members reached", value=f"{guilds['members']:,}")
        embed.add_field(name="Joined (24h / 7d)", value=f"{guilds['joined_day']} / {guilds['joined_week']}")
        embed.add_field(name="Left (7d)", value=str(guilds["left_week"]))
        embed.add_field(name="Giveaways", value=f"{totals['giveaways']:,} ({totals['active']} running)")
        embed.add_field(name="Entries", value=f"{totals['entries']:,}")
        embed.add_field(name="Votes", value=f"{votes['votes']:,} from {votes['voters']:,} people")
        if top:
            embed.add_field(name="Biggest servers", inline=False,
                            value="\n".join(f"{r['name']}: {r['member_count']:,}" for r in top)[:1000])
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    cog = Growth(bot)
    await bot.add_cog(cog)
    if cog.topgg_token:  # only offer /vote once the bot is listed on top.gg
        @bot.tree.command(name="vote", description="Support Giftly by voting for it")
        async def vote(interaction: discord.Interaction):
            await cog.send_vote(interaction)
