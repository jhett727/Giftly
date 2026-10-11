"""Extra giveaway features that are mixed into the main giveaway cog: the create form,
templates, recurring giveaways, drops, activity requirements, stats and exports."""
import asyncio
import csv
import datetime as dt
import io
import logging
from collections import Counter, defaultdict
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import parse_duration

log = logging.getLogger("giftly.extras")

MIN_SECONDS = 30
MAX_SECONDS = 90 * 86400
SPARK = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


class LaunchError(Exception):
    pass


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def ts(when, style="f"):
    return f"<t:{int(when.timestamp())}:{style}>"


def ordinal(n):
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def parse_lines(text, limit, width):
    return [line.strip()[:width] for line in text.splitlines() if line.strip()][:limit]


def params_from_row(g):
    """Everything needed to launch the same giveaway again."""
    return {
        "prize": g["prize"], "seconds": max(int((g["ends_at"] - g["created_at"]).total_seconds()), MIN_SECONDS),
        "winners": g["winners"], "description": g["description"], "required_role_id": g["required_role_id"],
        "bonus_role_id": g["bonus_role_id"], "bonus_entries": g["bonus_entries"], "image_url": g["image_url"],
        "min_account_days": g["min_account_days"], "min_server_days": g["min_server_days"],
        "prize_list": list(g["prize_list"]), "require_vote": g["require_vote"], "min_messages": g["min_messages"],
    }


class DropView(discord.ui.View):
    def __init__(self, cog, label="Grab it"):
        super().__init__(timeout=None)
        self.cog = cog
        self.grab.label = label

    @discord.ui.button(label="Grab it", emoji="\U0001F4A8", style=discord.ButtonStyle.success,
                       custom_id="giftly:drop")
    async def grab(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_drop(interaction)


class CreateModal(discord.ui.Modal, title="New giveaway"):
    def __init__(self, cog, opts, cfg):
        super().__init__()
        self.cog, self.opts = cog, opts
        self.prize = discord.ui.TextInput(
            label="Prize (one per line for several prizes)", style=discord.TextStyle.paragraph, max_length=500)
        self.duration = discord.ui.TextInput(
            label="Duration", placeholder="e.g. 2h or 1d12h", max_length=20,
            default=cfg["default_duration"], required=not cfg["default_duration"])
        self.winners = discord.ui.TextInput(
            label="Winners (ignored with several prizes)", max_length=2, required=False,
            default=str(cfg["default_winners"]))
        self.description = discord.ui.TextInput(
            label="Description", style=discord.TextStyle.paragraph, required=False, max_length=500)
        self.codes = discord.ui.TextInput(
            label="Prize codes, one per line (optional)", style=discord.TextStyle.paragraph,
            required=False, max_length=1500)
        for item in (self.prize, self.duration, self.winners, self.description, self.codes):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cog, opts = self.cog, self.opts
        prizes = parse_lines(str(self.prize), 10, 100)
        if not prizes:
            return await cog.say(interaction, "Add a prize first.")
        cfg = await cog.settings(interaction.guild_id)
        duration = str(self.duration).strip() or cfg["default_duration"] or ""
        seconds = parse_duration(duration) if duration else None
        if seconds is None or not MIN_SECONDS <= seconds <= MAX_SECONDS:
            return await cog.say(interaction, "Use a duration like `30m`, `2h` or `1d12h` (30 seconds to 90 days).")
        raw = str(self.winners).strip()
        if raw and not (raw.isdigit() and 1 <= int(raw) <= 20):
            return await cog.say(interaction, "Winners needs to be a number from 1 to 20.")
        winners = int(raw) if raw else cfg["default_winners"]
        channel = await cog.resolve_channel(interaction, opts["channel"])
        if channel is None:
            return
        if len(prizes) > 1:
            prize_list, winners, headline = prizes, len(prizes), f"{prizes[0]} +{len(prizes) - 1} more"[:100]
        else:
            prize_list, headline = [], prizes[0]
        params = dict(opts["params"], prize=headline, seconds=seconds, winners=winners,
                      description=str(self.description).strip() or None, prize_list=prize_list,
                      codes=parse_lines(str(self.codes), 100, 200))
        try:
            g, msg = await cog.launch(interaction.guild, channel, interaction.user.id, params)
        except LaunchError as exc:
            return await cog.say(interaction, str(exc))
        await interaction.followup.send(
            f"Giveaway #{g['id']} is live in {channel.mention}. [Jump to it]({msg.jump_url})", ephemeral=True)


class ExtrasMixin:
    # ------------------------------------------------------------------ lifecycle
    def extras_init(self):
        self.buffer = Counter()
        self.tracked = {}
        self.by_guild = defaultdict(set)
        self.repeats = {}

    async def extras_load(self):
        for row in await self.db.tracked_giveaways():
            self.track(row["id"], row["guild_id"])
        for row in await self.db.repeat_schedule():
            self.repeats[row["id"]] = row["next_run"]
        self.activity_loop.start()
        self.repeat_loop.start()

    def extras_unload(self):
        self.activity_loop.cancel()
        self.repeat_loop.cancel()

    # ------------------------------------------------------------------ message activity requirement
    def track(self, gid, guild_id):
        self.tracked[gid] = guild_id
        self.by_guild[guild_id].add(gid)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild is None or message.author.bot:
            return
        for gid in self.by_guild.get(message.guild.id, ()):
            self.buffer[(gid, message.author.id)] += 1

    @tasks.loop(seconds=30)
    async def activity_loop(self):
        for gid in [g for g in self.tracked if g not in self.schedule]:
            self.by_guild[self.tracked.pop(gid)].discard(gid)
        if not self.buffer:
            return
        batch, self.buffer = self.buffer, Counter()
        try:
            await self.db.bump_activity([(gid, uid, n) for (gid, uid), n in batch.items()])
        except Exception:
            log.exception("Could not save message counts")
            self.buffer.update(batch)

    @activity_loop.before_loop
    async def _before_activity(self):
        await self.bot.wait_until_ready()

    async def message_count(self, gid, uid):
        return await self.db.activity_count(gid, uid) + self.buffer.get((gid, uid), 0)

    # ------------------------------------------------------------------ recurring giveaways
    @tasks.loop(seconds=30)
    async def repeat_loop(self):
        now = utcnow()
        for rid, due in list(self.repeats.items()):
            if due <= now:
                self.repeats[rid] = now + dt.timedelta(minutes=10)  # hold off until this run finishes
                self.spawn(self._run_repeat(rid))

    @repeat_loop.before_loop
    async def _before_repeat(self):
        await self.bot.wait_until_ready()

    async def _run_repeat(self, rid):
        try:
            row = await self.db.repeat_get(rid)
            if row is None or not row["active"]:
                self.repeats.pop(rid, None)
                return
            channel = self.bot.get_channel(row["channel_id"])
            guild = self.bot.get_guild(row["guild_id"])
            if channel is None or guild is None:
                log.warning("Recurring giveaway %s skipped: channel or server not reachable", rid)
            else:
                await self.launch(guild, channel, row["created_by"], dict(row["data"]))
        except LaunchError as exc:
            log.warning("Recurring giveaway %s skipped: %s", rid, exc)
        except Exception:
            log.exception("Recurring giveaway %s failed", rid)
        finally:
            if rid in self.repeats:
                next_run = utcnow() + dt.timedelta(seconds=row["every_seconds"] if row else 3600)
                self.repeats[rid] = next_run
                try:
                    await self.db.repeat_advance(rid, next_run)
                except Exception:
                    log.exception("Could not schedule the next run of %s", rid)

    # ------------------------------------------------------------------ shared helpers
    async def resolve_channel(self, interaction, channel):
        cfg = await self.settings(interaction.guild_id)
        if channel is None and cfg["default_channel_id"]:
            channel = interaction.guild.get_channel(cfg["default_channel_id"])
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await self.say(interaction, "Pick a regular text channel for this giveaway.")
            return None
        perms = channel.permissions_for(interaction.guild.me)
        lacking = [name for name, ok in (("View Channel", perms.view_channel), ("Send Messages", perms.send_messages),
                                         ("Embed Links", perms.embed_links)) if not ok]
        if lacking:
            await self.say(interaction, f"I'm missing these permissions in {channel.mention}: {', '.join(lacking)}.")
            return None
        return channel

    # ------------------------------------------------------------------ create form and drops
    @app_commands.command(name="create", description="Create a giveaway with a form")
    @app_commands.describe(channel="Where to post it", required_role="Only members with this role can enter",
                           bonus_role="Members with this role get extra entries",
                           bonus_entries="Extra entries for the bonus role (default 1)",
                           require_vote="Entrants must have voted for Giftly on top.gg in the last 12 hours",
                           min_messages="Messages entrants must send while the giveaway runs")
    async def gw_create(self, interaction: discord.Interaction,
                        channel: Optional[discord.TextChannel] = None,
                        required_role: Optional[discord.Role] = None,
                        bonus_role: Optional[discord.Role] = None,
                        bonus_entries: app_commands.Range[int, 1, 50] = 1,
                        require_vote: bool = False,
                        min_messages: Optional[app_commands.Range[int, 1, 10000]] = None):
        cached = self._cfg.get(interaction.guild_id)
        cfg = cached[1] if cached else {"default_duration": None, "default_winners": 1}
        params = {"required_role_id": required_role.id if required_role else None,
                  "bonus_role_id": bonus_role.id if bonus_role else None,
                  "bonus_entries": bonus_entries if bonus_role else 0,
                  "require_vote": require_vote, "min_messages": min_messages or 0}
        await interaction.response.send_modal(CreateModal(self, {"channel": channel, "params": params}, cfg))

    @app_commands.command(name="drop", description="Drop a prize: the first person to press the button wins")
    @app_commands.describe(prize="What's up for grabs", minutes="How long the drop stays up (default 10)",
                           channel="Where to post it", required_role="Only members with this role can grab it")
    async def gw_drop(self, interaction: discord.Interaction, prize: app_commands.Range[str, 1, 100],
                      minutes: app_commands.Range[int, 1, 60] = 10,
                      channel: Optional[discord.TextChannel] = None,
                      required_role: Optional[discord.Role] = None):
        await interaction.response.defer(ephemeral=True)
        channel = await self.resolve_channel(interaction, channel)
        if channel is None:
            return
        params = {"prize": prize, "seconds": minutes * 60, "winners": 1, "is_drop": True,
                  "required_role_id": required_role.id if required_role else None}
        try:
            g, msg = await self.launch(interaction.guild, channel, interaction.user.id, params)
        except LaunchError as exc:
            return await self.say(interaction, str(exc))
        await interaction.followup.send(f"The drop is live in {channel.mention}. [Jump to it]({msg.jump_url})", ephemeral=True)

    # ------------------------------------------------------------------ templates
    @app_commands.command(name="template-save", description="Save a giveaway's setup as a template")
    @app_commands.describe(name="A short name for the template", giveaway="The giveaway to copy the setup from")
    async def gw_template_save(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 30],
                               giveaway: int):
        g = await self.fetch_owned(interaction, giveaway)
        if g is None:
            return
        await interaction.response.defer(ephemeral=True)
        name = name.strip()
        existing = {r["name"] for r in await self.db.template_list(interaction.guild_id)}
        if name not in existing and len(existing) >= 15:
            return await self.say(interaction, "You can keep up to 15 templates. Delete one first.")
        await self.db.template_save(interaction.guild_id, name, params_from_row(g))
        await interaction.followup.send(f"Saved the template **{name}**. Use it with `/giveaway template-use`.", ephemeral=True)

    @app_commands.command(name="template-use", description="Start a giveaway from a saved template")
    @app_commands.describe(name="Which template", channel="Where to post it", duration="Use a different duration, e.g. 2d")
    async def gw_template_use(self, interaction: discord.Interaction, name: str,
                              channel: Optional[discord.TextChannel] = None, duration: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        row = await self.db.template_get(interaction.guild_id, name)
        if row is None:
            return await self.say(interaction, "I couldn't find a template with that name.")
        data = dict(row["data"])
        if duration:
            seconds = parse_duration(duration)
            if seconds is None or not MIN_SECONDS <= seconds <= MAX_SECONDS:
                return await self.say(interaction, "Use a duration like `30m`, `2h` or `1d12h`.")
            data["seconds"] = seconds
        channel = await self.resolve_channel(interaction, channel)
        if channel is None:
            return
        try:
            g, msg = await self.launch(interaction.guild, channel, interaction.user.id, data)
        except LaunchError as exc:
            return await self.say(interaction, str(exc))
        await interaction.followup.send(f"Giveaway #{g['id']} is live in {channel.mention}. [Jump to it]({msg.jump_url})", ephemeral=True)

    @app_commands.command(name="template-list", description="Show your saved templates")
    async def gw_template_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await self.db.template_list(interaction.guild_id)
        if not rows:
            return await interaction.followup.send("No templates yet. Save one with `/giveaway template-save`.", ephemeral=True)
        lines = [f"**{r['name']}**: {r['data']['prize']}, {r['data']['winners']} winner(s)" for r in rows]
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)

    @app_commands.command(name="template-delete", description="Delete a saved template")
    @app_commands.describe(name="Which template")
    async def gw_template_delete(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)
        done = await self.db.template_delete(interaction.guild_id, name)
        await interaction.followup.send("Deleted." if done else "I couldn't find a template with that name.", ephemeral=True)

    # ------------------------------------------------------------------ recurring commands
    @app_commands.command(name="repeat-start", description="Run a saved template again and again")
    @app_commands.describe(template="Which template to repeat", every="How often, e.g. 1d or 7d (at least 1 hour)",
                           channel="Where to post it")
    async def gw_repeat_start(self, interaction: discord.Interaction, template: str, every: str,
                              channel: Optional[discord.TextChannel] = None):
        await interaction.response.defer(ephemeral=True)
        seconds = parse_duration(every)
        if seconds is None or not 3600 <= seconds <= MAX_SECONDS:
            return await self.say(interaction, "Use something like `1d` or `7d`, at least an hour.")
        row = await self.db.template_get(interaction.guild_id, template)
        if row is None:
            return await self.say(interaction, "I couldn't find a template with that name.")
        if await self.db.repeat_count(interaction.guild_id) >= 5:
            return await self.say(interaction, "You can have up to 5 repeating giveaways. Stop one first.")
        channel = await self.resolve_channel(interaction, channel)
        if channel is None:
            return
        run = await self.db.repeat_create(interaction.guild_id, channel.id, template, row["data"],
                                          seconds, utcnow(), interaction.user.id)
        self.repeats[run["id"]] = utcnow()  # the first one goes out right away
        await interaction.followup.send(
            f"Repeating **{template}** in {channel.mention} every {every.strip()}. The first one is on its way. "
            f"Stop it any time with `/giveaway repeat-stop`.", ephemeral=True)

    @app_commands.command(name="repeat-list", description="Show repeating giveaways")
    async def gw_repeat_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await self.db.repeat_list(interaction.guild_id)
        if not rows:
            return await interaction.followup.send("Nothing is repeating right now.", ephemeral=True)
        lines = [f"**#{r['id']}** {r['name']} in <#{r['channel_id']}>, every {r['every_seconds'] // 3600}h, next {ts(r['next_run'], 'R')}"
                 for r in rows]
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)

    @app_commands.command(name="repeat-stop", description="Stop a repeating giveaway")
    @app_commands.describe(repeat="Which one to stop")
    async def gw_repeat_stop(self, interaction: discord.Interaction, repeat: int):
        await interaction.response.defer(ephemeral=True)
        done = await self.db.repeat_stop(repeat, interaction.guild_id)
        self.repeats.pop(repeat, None)
        await interaction.followup.send("Stopped." if done else "I couldn't find that one.", ephemeral=True)

    # ------------------------------------------------------------------ stats, exports, prize codes
    @app_commands.command(name="stats", description="See how a giveaway is doing")
    @app_commands.describe(giveaway="Which giveaway")
    async def gw_stats(self, interaction: discord.Interaction, giveaway: int):
        g = await self.fetch_owned(interaction, giveaway)
        if g is None:
            return
        await interaction.response.defer(ephemeral=True)
        rows = await self.db.entry_rows(giveaway)
        start, end = g["created_at"], min(g["ends_at"], utcnow())
        span = max((end - start).total_seconds(), 1)
        buckets = [0] * 12
        for r in rows:
            index = int((r["joined_at"] - start).total_seconds() / span * 12)
            buckets[min(max(index, 0), 11)] += 1
        peak = max(buckets)
        spark = "".join(SPARK[min(int(b / peak * 7), 7)] if peak else SPARK[0] for b in buckets)
        cfg = await self.settings(g["guild_id"])
        embed = discord.Embed(title=f"Stats for #{giveaway}: {g['prize']}", color=cfg["color"],
                              description=f"Entries over the giveaway\n`{spark}`")
        embed.add_field(name="Entrants", value=f"{len(rows):,}")
        embed.add_field(name="Total entries", value=f"{sum(r['weight'] for r in rows):,}")
        embed.add_field(name="With bonus entries", value=f"{sum(1 for r in rows if r['weight'] > 1):,}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="mystats", description="See your giveaway history in this server")
    @app_commands.describe(user="Look up someone else")
    async def gw_mystats(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        target = user or interaction.user
        await interaction.response.defer(ephemeral=True)
        entered, won, recent = await self.db.user_stats(interaction.guild_id, target.id)
        cfg = await self.settings(interaction.guild_id)
        embed = discord.Embed(title=f"Giveaway history for {target.display_name}", color=cfg["color"])
        embed.add_field(name="Entered", value=f"{entered:,}")
        embed.add_field(name="Won", value=f"{won:,}")
        if recent:
            embed.add_field(name="Recent wins", inline=False,
                            value="\n".join(f"{r['prize']} ({ts(r['ends_at'], 'd')})" for r in recent))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="entrants", description="Download everyone who entered as a spreadsheet file")
    @app_commands.describe(giveaway="Which giveaway")
    async def gw_entrants(self, interaction: discord.Interaction, giveaway: int):
        g = await self.fetch_owned(interaction, giveaway)
        if g is None:
            return
        await interaction.response.defer(ephemeral=True)
        rows = await self.db.entry_rows(giveaway)
        if not rows:
            return await interaction.followup.send("Nobody has entered yet.", ephemeral=True)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["user_id", "entries", "joined_at"])
        for r in rows:
            writer.writerow([r["user_id"], r["weight"], r["joined_at"].isoformat()])
        file = discord.File(io.BytesIO(out.getvalue().encode()), filename=f"giveaway-{giveaway}-entrants.csv")
        await interaction.followup.send(f"{len(rows):,} entrants.", file=file, ephemeral=True)

    @app_commands.command(name="awards", description="See the prize codes that were handed out")
    @app_commands.describe(giveaway="Which giveaway")
    async def gw_awards(self, interaction: discord.Interaction, giveaway: int):
        g = await self.fetch_owned(interaction, giveaway)
        if g is None:
            return
        await interaction.response.defer(ephemeral=True)
        rows = await self.db.awards(giveaway)
        if not rows:
            return await interaction.followup.send("No prize codes were handed out for that giveaway.", ephemeral=True)
        lines = [f"<@{r['user_id']}> | {r['prize']} | ||{r['code']}|| | {'sent' if r['delivered'] else 'NOT sent'}" for r in rows]
        cfg = await self.settings(g["guild_id"])
        embed = discord.Embed(title=f"Prize codes for #{giveaway}", description="\n".join(lines)[:4000], color=cfg["color"])
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ autocomplete
    async def _template_choices(self, interaction, current):
        try:
            if not await self.is_manager(interaction):
                return []
            rows = await self.db.template_list(interaction.guild_id)
        except Exception:
            return []
        current = current.lower()
        return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows if current in r["name"].lower()][:25]

    @gw_template_use.autocomplete("name")
    @gw_template_delete.autocomplete("name")
    @gw_repeat_start.autocomplete("template")
    async def ac_templates(self, interaction: discord.Interaction, current: str):
        return await self._template_choices(interaction, current)

    @gw_repeat_stop.autocomplete("repeat")
    async def ac_repeats(self, interaction: discord.Interaction, current: str):
        try:
            if not await self.is_manager(interaction):
                return []
            rows = await self.db.repeat_list(interaction.guild_id)
        except Exception:
            return []
        return [app_commands.Choice(name=f"#{r['id']} {r['name']}"[:100], value=r["id"]) for r in rows][:25]

    @gw_template_save.autocomplete("giveaway")
    @gw_stats.autocomplete("giveaway")
    @gw_entrants.autocomplete("giveaway")
    @gw_awards.autocomplete("giveaway")
    async def ac_any_giveaway(self, interaction: discord.Interaction, current: str):
        return await self._choices(interaction, current, "all")
