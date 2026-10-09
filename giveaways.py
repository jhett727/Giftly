import asyncio
import datetime as dt
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import parse_duration, weighted_order

log = logging.getLogger("giftly.giveaways")

DEFAULT_COLOR = 0x7C3AED
ENDED_COLOR = 0x4B5563
CANCELLED_COLOR = 0x991B1B
MIN_SECONDS = 30
MAX_SECONDS = 90 * 86400
WINNER_MENTIONS = discord.AllowedMentions(users=True, roles=False, everyone=False)


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def ts(when, style="f"):
    return f"<t:{int(when.timestamp())}:{style}>"


def jump(g):
    return f"https://discord.com/channels/{g['guild_id']}/{g['channel_id']}/{g['message_id']}"


def tidy(text):
    return text.replace("[", "(").replace("]", ")")


def closed_view(label):
    view = discord.ui.View(timeout=60)
    view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=True))
    return view


class EntryView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Enter", emoji="\U0001F389", style=discord.ButtonStyle.primary,
                       custom_id="giftly:enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_enter(interaction)


class LeaveView(discord.ui.View):
    def __init__(self, cog, gid, uid):
        super().__init__(timeout=60)
        self.cog, self.gid, self.uid = cog, gid, uid

    @discord.ui.button(label="Leave giveaway", style=discord.ButtonStyle.danger)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        removed = await self.cog.db.remove_entry(self.gid, self.uid)
        if removed:
            self.cog.schedule_refresh(self.gid)
        await interaction.edit_original_response(
            content="You've left the giveaway." if removed else "You weren't entered.", view=None)


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class Giveaways(commands.GroupCog, group_name="giveaway",
                group_description="Create and manage giveaways"):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.db = bot.db
        self.schedule = {}          # giveaway id -> end time, kept in memory so we never poll the database
        self._ending = set()
        self._refresh = {}
        self._tasks = set()

    # ------------------------------------------------------------------ lifecycle
    async def cog_load(self):
        self.bot.add_view(EntryView(self))
        for row in await self.db.active_schedule():
            self.schedule[row["id"]] = row["ends_at"]
        self.watcher.start()

    async def cog_unload(self):
        self.watcher.cancel()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @tasks.loop(seconds=5)
    async def watcher(self):
        now = utcnow()
        for gid, ends_at in list(self.schedule.items()):
            if ends_at <= now and gid not in self._ending:
                self.spawn(self._end_safely(gid))

    @watcher.before_loop
    async def _before_watcher(self):
        await self.bot.wait_until_ready()

    async def _end_safely(self, gid):
        try:
            await self.end_giveaway(gid)
        except Exception:
            log.exception("Failed to end giveaway %s, retrying in a minute", gid)
            self.schedule[gid] = utcnow() + dt.timedelta(seconds=60)

    # ------------------------------------------------------------------ helpers
    async def guild_color(self, guild_id):
        settings = await self.db.get_settings(guild_id)
        return settings["color"] if settings else DEFAULT_COLOR

    async def get_channel(self, channel_id):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return channel

    @staticmethod
    def partial(channel, g):
        if channel is None or g["message_id"] is None:
            return None
        return channel.get_partial_message(g["message_id"])

    @staticmethod
    async def say(interaction, text, **kwargs):
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True, **kwargs)
        else:
            await interaction.response.send_message(text, ephemeral=True, **kwargs)

    async def fetch_owned(self, interaction, gid):
        g = await self.db.get(gid)
        if g is None or g["guild_id"] != interaction.guild_id:
            await self.say(interaction, "I couldn't find that giveaway.")
            return None
        return g

    def build_embed(self, g, count, color, state="active"):
        ends = g["ends_at"]
        parts = []
        if g["description"]:
            parts.append(g["description"])
        if state == "active":
            parts.append(f"Ends {ts(ends, 'R')} ({ts(ends, 'f')})")
        elif state == "ended":
            parts.append(f"Ended {ts(ends, 'R')}")
        else:
            parts.append("This giveaway was cancelled.")

        shade = {"active": color, "ended": ENDED_COLOR, "cancelled": CANCELLED_COLOR}[state]
        embed = discord.Embed(title=g["prize"], description="\n\n".join(parts), color=shade)
        embed.set_author(name={"active": "Giveaway", "ended": "Giveaway ended",
                               "cancelled": "Giveaway cancelled"}[state])
        embed.add_field(name="Hosted by", value=f"<@{g['host_id']}>")
        if state == "ended":
            winners = ", ".join(f"<@{u}>" for u in g["winner_ids"]) or "No valid entries"
            embed.add_field(name="Winners", value=winners[:1000])
        else:
            embed.add_field(name="Winners", value=str(g["winners"]))
        embed.add_field(name="Entries", value=f"{count:,}")

        if state == "active":
            lines = []
            if g["required_role_id"]:
                lines.append(f"You need <@&{g['required_role_id']}> to enter.")
            if g["bonus_role_id"] and g["bonus_entries"]:
                lines.append(f"<@&{g['bonus_role_id']}> gets +{g['bonus_entries']} bonus "
                             f"{'entry' if g['bonus_entries'] == 1 else 'entries'}.")
            if lines:
                embed.add_field(name="Details", value="\n".join(lines), inline=False)
            embed.set_footer(text=f"Giveaway #{g['id']}")
        else:
            embed.set_footer(text=f"Giveaway #{g['id']}")
        embed.timestamp = ends
        return embed

    # ------------------------------------------------------------------ message updates
    def schedule_refresh(self, gid):
        """Coalesce rapid joins into one embed edit so we stay clear of rate limits."""
        task = self._refresh.get(gid)
        if task and not task.done():
            return
        self._refresh[gid] = asyncio.create_task(self._delayed_refresh(gid))

    async def _delayed_refresh(self, gid):
        await asyncio.sleep(3)
        try:
            await self.refresh_message(gid)
        except Exception:
            log.exception("Could not refresh giveaway %s", gid)

    async def refresh_message(self, gid):
        g = await self.db.get(gid)
        if g is None or g["ended"] or g["cancelled"]:
            return
        msg = self.partial(await self.get_channel(g["channel_id"]), g)
        if msg is None:
            return
        count = await self.db.entry_count(gid)
        embed = self.build_embed(g, count, await self.guild_color(g["guild_id"]))
        try:
            await msg.edit(embed=embed)
        except discord.HTTPException:
            pass

    async def render_final(self, g):
        state = "cancelled" if g["cancelled"] else "ended"
        msg = self.partial(await self.get_channel(g["channel_id"]), g)
        if msg is None:
            return
        count = await self.db.entry_count(g["id"])
        embed = self.build_embed(g, count, await self.guild_color(g["guild_id"]), state)
        try:
            await msg.edit(embed=embed, view=closed_view("Cancelled" if state == "cancelled" else "Ended"))
        except discord.HTTPException:
            log.warning("Could not edit message for giveaway %s", g["id"])

    async def announce(self, g, text):
        channel = await self.get_channel(g["channel_id"])
        if channel is None:
            return
        msg = self.partial(channel, g)
        try:
            if msg is not None:
                await msg.reply(text, allowed_mentions=WINNER_MENTIONS)
            else:
                await channel.send(text, allowed_mentions=WINNER_MENTIONS)
        except discord.HTTPException:
            try:
                await channel.send(text, allowed_mentions=WINNER_MENTIONS)
            except discord.HTTPException:
                pass

    async def dm_winner(self, uid, g, guild_name):
        try:
            user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            embed = discord.Embed(
                title="You won!",
                description=f"You won **{g['prize']}** in **{guild_name}**.\n[Jump to the giveaway]({jump(g)})",
                color=DEFAULT_COLOR)
            await user.send(embed=embed)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------ drawing
    async def pick_winners(self, guild, g, entries, k, exclude):
        """Draw in weighted random order, checking each candidate is still eligible."""
        order = weighted_order([(u, w) for u, w in entries if u not in exclude])
        winners, checked = [], 0
        for uid in order:
            if len(winners) >= k or checked >= 250:
                break
            checked += 1
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await guild.fetch_member(uid)
                except discord.HTTPException:
                    continue
            if member.bot:
                continue
            if g["required_role_id"] and member.get_role(g["required_role_id"]) is None:
                continue
            winners.append(uid)
        return winners

    async def end_giveaway(self, gid):
        if gid in self._ending:
            return None
        self._ending.add(gid)
        try:
            g = await self.db.get(gid)
            if g is None or g["ended"] or g["cancelled"]:
                self.schedule.pop(gid, None)
                return None
            guild = self.bot.get_guild(g["guild_id"])
            entries = await self.db.entries(gid)
            winners = await self.pick_winners(guild, g, entries, g["winners"], set()) if guild else []
            g = await self.db.finish(gid, winners)
            self.schedule.pop(gid, None)
            await self.render_final(g)
            if winners:
                names = ", ".join(f"<@{u}>" for u in winners)
                await self.announce(g, f"Congratulations {names}! You won **{g['prize']}**.")
                for uid in winners:
                    await self.dm_winner(uid, g, guild.name)
            else:
                await self.announce(g, f"Nobody entered **{g['prize']}** with a valid entry, so there's no winner.")
            return g
        finally:
            self._ending.discard(gid)

    # ------------------------------------------------------------------ entering
    async def handle_enter(self, interaction):
        await interaction.response.defer(ephemeral=True)
        g = await self.db.get_by_message(interaction.message.id)
        if g is None or g["ended"] or g["cancelled"] or g["ends_at"] <= utcnow():
            return await interaction.followup.send("This giveaway is no longer taking entries.", ephemeral=True)

        member = interaction.user
        if g["required_role_id"] and member.get_role(g["required_role_id"]) is None:
            return await interaction.followup.send(
                f"You need the <@&{g['required_role_id']}> role to enter this one.", ephemeral=True)

        weight = 1
        if g["bonus_role_id"] and member.get_role(g["bonus_role_id"]) is not None:
            weight += g["bonus_entries"]

        if await self.db.add_entry(g["id"], member.id, weight):
            self.schedule_refresh(g["id"])
            text = "You're in. Good luck!"
            if weight > 1:
                text += f" Your role gives you {weight} entries."
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.followup.send(
                "You're already entered. Want to leave?",
                view=LeaveView(self, g["id"], member.id), ephemeral=True)

    # ------------------------------------------------------------------ autocomplete
    async def _choices(self, interaction, current, mode):
        try:
            rows = await self.db.search(interaction.guild_id, mode)
        except Exception:
            return []
        current = current.lower()
        out = []
        for r in rows:
            label = f"#{r['id']} · {r['prize']}"
            if current and current not in label.lower():
                continue
            out.append(app_commands.Choice(name=label[:100], value=r["id"]))
            if len(out) == 25:
                break
        return out

    # ------------------------------------------------------------------ commands
    @app_commands.command(name="start", description="Start a giveaway")
    @app_commands.describe(
        prize="What's being given away",
        duration="How long it runs, e.g. 30m, 2h, 1d12h",
        winners="How many winners (default 1)",
        channel="Where to post it (default: this channel)",
        required_role="Only members with this role can enter",
        bonus_role="Members with this role get extra entries",
        bonus_entries="How many extra entries the bonus role gets (default 1)",
        description="Extra text shown on the giveaway")
    async def gw_start(self, interaction: discord.Interaction,
                       prize: app_commands.Range[str, 1, 100],
                       duration: str,
                       winners: app_commands.Range[int, 1, 20] = 1,
                       channel: Optional[discord.TextChannel] = None,
                       required_role: Optional[discord.Role] = None,
                       bonus_role: Optional[discord.Role] = None,
                       bonus_entries: app_commands.Range[int, 1, 50] = 1,
                       description: Optional[app_commands.Range[str, 1, 500]] = None):
        seconds = parse_duration(duration)
        if seconds is None:
            return await self.say(interaction, "I couldn't read that duration. Try something like `30m`, `2h` or `1d12h`.")
        if seconds < MIN_SECONDS:
            return await self.say(interaction, "Giveaways need to run for at least 30 seconds.")
        if seconds > MAX_SECONDS:
            return await self.say(interaction, "Giveaways can run for 90 days at most.")

        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await self.say(interaction, "Pick a regular text channel for this giveaway.")
        perms = channel.permissions_for(interaction.guild.me)
        lacking = [name for name, ok in (("View Channel", perms.view_channel),
                                         ("Send Messages", perms.send_messages),
                                         ("Embed Links", perms.embed_links)) if not ok]
        if lacking:
            return await self.say(interaction, f"I'm missing these permissions in {channel.mention}: {', '.join(lacking)}.")

        await interaction.response.defer(ephemeral=True)
        g = await self.db.create(
            guild_id=interaction.guild_id, channel_id=channel.id, host_id=interaction.user.id,
            prize=prize, description=description, winners=winners,
            ends_at=utcnow() + dt.timedelta(seconds=seconds),
            required_role_id=required_role.id if required_role else None,
            bonus_role_id=bonus_role.id if bonus_role else None,
            bonus_entries=bonus_entries if bonus_role else 0)

        settings = await self.db.get_settings(interaction.guild_id)
        ping = f"<@&{settings['ping_role_id']}>" if settings and settings["ping_role_id"] else None
        color = settings["color"] if settings else DEFAULT_COLOR
        try:
            msg = await channel.send(
                content=ping, embed=self.build_embed(g, 0, color), view=EntryView(self),
                allowed_mentions=discord.AllowedMentions(roles=True) if ping else None)
        except discord.HTTPException:
            await self.db.delete(g["id"])
            return await self.say(interaction, "I couldn't post in that channel. Check my permissions and try again.")

        await self.db.set_message(g["id"], msg.id)
        self.schedule[g["id"]] = g["ends_at"]
        await interaction.followup.send(
            f"Giveaway #{g['id']} is live in {channel.mention}. [Jump to it]({msg.jump_url})", ephemeral=True)

    @app_commands.command(name="end", description="End a giveaway early and draw the winners")
    @app_commands.describe(giveaway="Which giveaway to end")
    async def gw_end(self, interaction: discord.Interaction, giveaway: int):
        g = await self.fetch_owned(interaction, giveaway)
        if g is None:
            return
        if g["ended"] or g["cancelled"]:
            return await self.say(interaction, "That giveaway has already finished.")
        await interaction.response.defer(ephemeral=True)
        await self.end_giveaway(giveaway)
        await interaction.followup.send("Done. The winners have been drawn.", ephemeral=True)

    @app_commands.command(name="reroll", description="Draw a new winner for a finished giveaway")
    @app_commands.describe(giveaway="Which giveaway to reroll",
                           count="How many extra winners to draw (default 1)",
                           replace="Swap out this winner instead of adding more")
    async def gw_reroll(self, interaction: discord.Interaction, giveaway: int,
                        count: app_commands.Range[int, 1, 20] = 1,
                        replace: Optional[discord.User] = None):
        g = await self.fetch_owned(interaction, giveaway)
        if g is None:
            return
        if not g["ended"]:
            return await self.say(interaction, "That giveaway is still running. Use `/giveaway end` first.")
        if replace is not None and replace.id not in g["winner_ids"]:
            return await self.say(interaction, f"{replace.mention} isn't one of the winners of that giveaway.")
        guild = interaction.guild
        await interaction.response.defer(ephemeral=True)

        entries = await self.db.entries(giveaway)
        picked = await self.pick_winners(guild, g, entries, 1 if replace else count, set(g["winner_ids"]))
        if not picked:
            return await interaction.followup.send("There are no other eligible entries left to draw from.", ephemeral=True)

        kept = [u for u in g["winner_ids"] if not replace or u != replace.id]
        g = await self.db.set_winners(giveaway, kept + picked)
        await self.render_final(g)
        names = ", ".join(f"<@{u}>" for u in picked)
        await self.announce(g, f"New {'winner' if len(picked) == 1 else 'winners'} for **{g['prize']}**: {names}. Congratulations!")
        for uid in picked:
            await self.dm_winner(uid, g, guild.name)
        await interaction.followup.send("Rerolled.", ephemeral=True)

    @app_commands.command(name="cancel", description="Cancel a running giveaway without picking winners")
    @app_commands.describe(giveaway="Which giveaway to cancel")
    async def gw_cancel(self, interaction: discord.Interaction, giveaway: int):
        g = await self.fetch_owned(interaction, giveaway)
        if g is None:
            return
        if g["ended"] or g["cancelled"]:
            return await self.say(interaction, "That giveaway has already finished.")
        await interaction.response.defer(ephemeral=True)
        g = await self.db.cancel(giveaway)
        self.schedule.pop(giveaway, None)
        await self.render_final(g)
        await interaction.followup.send(f"Giveaway #{giveaway} was cancelled.", ephemeral=True)

    @app_commands.command(name="edit", description="Change the prize, winner count or end time")
    @app_commands.describe(giveaway="Which giveaway to edit", prize="New prize",
                           winners="New number of winners",
                           extend="Add time, e.g. 30m or 1d")
    async def gw_edit(self, interaction: discord.Interaction, giveaway: int,
                      prize: Optional[app_commands.Range[str, 1, 100]] = None,
                      winners: Optional[app_commands.Range[int, 1, 20]] = None,
                      extend: Optional[str] = None):
        g = await self.fetch_owned(interaction, giveaway)
        if g is None:
            return
        if g["ended"] or g["cancelled"]:
            return await self.say(interaction, "Only running giveaways can be edited.")
        if prize is None and winners is None and extend is None:
            return await self.say(interaction, "Tell me what to change: prize, winners or extend.")

        ends_at = g["ends_at"]
        if extend:
            seconds = parse_duration(extend)
            if not seconds:
                return await self.say(interaction, "I couldn't read that time. Try `30m` or `1d`.")
            ends_at += dt.timedelta(seconds=seconds)
            if (ends_at - utcnow()).total_seconds() > MAX_SECONDS:
                return await self.say(interaction, "That would push the end more than 90 days out.")

        await interaction.response.defer(ephemeral=True)
        g = await self.db.update(giveaway, prize or g["prize"], winners or g["winners"], ends_at)
        self.schedule[giveaway] = g["ends_at"]
        await self.refresh_message(giveaway)
        await interaction.followup.send(f"Giveaway #{giveaway} updated.", ephemeral=True)

    @app_commands.command(name="list", description="Show the running giveaways in this server")
    async def gw_list(self, interaction: discord.Interaction):
        rows = await self.db.active(interaction.guild_id)
        if not rows:
            return await self.say(interaction, "There are no running giveaways right now.")
        lines = [f"**#{r['id']}** · [{tidy(r['prize'])}]({jump(r)}) · ends {ts(r['ends_at'], 'R')} · "
                 f"{r['entry_count']:,} {'entry' if r['entry_count'] == 1 else 'entries'}" for r in rows]
        embed = discord.Embed(title="Running giveaways", description="\n".join(lines),
                              color=await self.guild_color(interaction.guild_id))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="info", description="Show the details of a giveaway")
    @app_commands.describe(giveaway="Which giveaway")
    async def gw_info(self, interaction: discord.Interaction, giveaway: int):
        g = await self.fetch_owned(interaction, giveaway)
        if g is None:
            return
        count = await self.db.entry_count(giveaway)
        status = "Cancelled" if g["cancelled"] else "Ended" if g["ended"] else "Running"
        embed = discord.Embed(title=g["prize"], color=await self.guild_color(g["guild_id"]),
                              url=jump(g) if g["message_id"] else None)
        embed.add_field(name="Status", value=status)
        embed.add_field(name="Hosted by", value=f"<@{g['host_id']}>")
        embed.add_field(name="Channel", value=f"<#{g['channel_id']}>")
        embed.add_field(name="Winners", value=str(g["winners"]))
        embed.add_field(name="Entries", value=f"{count:,}")
        embed.add_field(name="Ends" if status == "Running" else "Ended", value=ts(g["ends_at"], "R"))
        if g["required_role_id"]:
            embed.add_field(name="Required role", value=f"<@&{g['required_role_id']}>")
        if g["bonus_role_id"]:
            embed.add_field(name="Bonus role", value=f"<@&{g['bonus_role_id']}> (+{g['bonus_entries']})")
        if g["winner_ids"]:
            embed.add_field(name="Winners drawn", value=", ".join(f"<@{u}>" for u in g["winner_ids"])[:1000],
                            inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="config", description="Set the ping role and embed color for this server")
    @app_commands.describe(ping_role="Role to ping when a giveaway starts",
                           color="Embed color as a hex code, e.g. #7C3AED",
                           clear_ping="Stop pinging a role on new giveaways")
    async def gw_config(self, interaction: discord.Interaction,
                        ping_role: Optional[discord.Role] = None,
                        color: Optional[str] = None,
                        clear_ping: bool = False):
        parsed = None
        if color:
            raw = color.strip().lstrip("#")
            try:
                if len(raw) != 6:
                    raise ValueError
                parsed = int(raw, 16)
            except ValueError:
                return await self.say(interaction, "The color needs to be a hex code like `#7C3AED`.")
        if ping_role or parsed is not None or clear_ping:
            await self.db.set_settings(interaction.guild_id, parsed,
                                       ping_role.id if ping_role else None, clear_ping)
        s = await self.db.get_settings(interaction.guild_id)
        current = s["color"] if s else DEFAULT_COLOR
        role = f"<@&{s['ping_role_id']}>" if s and s["ping_role_id"] else "None"
        embed = discord.Embed(title="Giveaway settings", color=current)
        embed.add_field(name="Ping role", value=role)
        embed.add_field(name="Color", value=f"#{current:06X}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # autocomplete: pick giveaways by name instead of typing IDs
    @gw_end.autocomplete("giveaway")
    @gw_cancel.autocomplete("giveaway")
    @gw_edit.autocomplete("giveaway")
    async def ac_active(self, interaction: discord.Interaction, current: str):
        return await self._choices(interaction, current, "active")

    @gw_reroll.autocomplete("giveaway")
    async def ac_ended(self, interaction: discord.Interaction, current: str):
        return await self._choices(interaction, current, "ended")

    @gw_info.autocomplete("giveaway")
    async def ac_all(self, interaction: discord.Interaction, current: str):
        return await self._choices(interaction, current, "all")


async def setup(bot):
    await bot.add_cog(Giveaways(bot))
