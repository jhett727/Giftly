import asyncio
import datetime as dt
import logging
import re
import time
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
DEFAULT_WIN = "Congratulations {winners}! You won **{prize}**."
DEFAULT_NONE = "Nobody entered **{prize}** with a valid entry, so there's no winner."
WINNER_MENTIONS = discord.AllowedMentions(users=True, roles=False, everyone=False)

DEFAULTS = {
    "color": DEFAULT_COLOR, "ping_role_id": None, "manager_role_id": None, "blacklist_role_id": None,
    "button_label": "Enter", "button_emoji": "\U0001F389", "footer_text": None,
    "win_message": DEFAULT_WIN, "no_winner_message": DEFAULT_NONE, "dm_winners": True,
}
CUSTOM_EMOJI = re.compile(r"^<a?:\w+:\d+>$")


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def ts(when, style="f"):
    return f"<t:{int(when.timestamp())}:{style}>"


def jump(g):
    return f"https://discord.com/channels/{g['guild_id']}/{g['channel_id']}/{g['message_id']}"


def tidy(text):
    return text.replace("[", "(").replace("]", ")")


def fill(template, **values):
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template[:1900]


def hex_color(raw):
    raw = raw.strip().lstrip("#")
    if len(raw) != 6:
        return None
    try:
        return int(raw, 16)
    except ValueError:
        return None


def valid_emoji(text):
    return bool(CUSTOM_EMOJI.match(text)) or (len(text) <= 8 and any(ord(c) > 127 for c in text))


def role_text(role_id):
    return f"<@&{role_id}>" if role_id else "None"


def closed_view(label):
    view = discord.ui.View(timeout=60)
    view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=True))
    return view


# ====================================================================== entry buttons
class EntryView(discord.ui.View):
    def __init__(self, cog, label="Enter", emoji="\U0001F389"):
        super().__init__(timeout=None)
        self.cog = cog
        self.enter.label = label
        self.enter.emoji = emoji or None

    @discord.ui.button(label="Enter", style=discord.ButtonStyle.primary, custom_id="giftly:enter")
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


# ====================================================================== settings panel
class RolePicker(discord.ui.RoleSelect):
    def __init__(self, cog, field, placeholder, current, row):
        defaults = ([discord.SelectDefaultValue(id=current, type=discord.SelectDefaultValueType.role)]
                    if current else [])
        super().__init__(placeholder=placeholder, min_values=0, max_values=1,
                         default_values=defaults, row=row)
        self.cog, self.field = cog, field

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        value = self.values[0].id if self.values else None
        await self.cog.save_setting(interaction.guild_id, self.field, value)
        await self.cog.refresh_panel(interaction)


class AppearanceModal(discord.ui.Modal, title="Appearance"):
    def __init__(self, cog, cfg):
        super().__init__()
        self.cog = cog
        self.color = discord.ui.TextInput(
            label="Embed color (hex)", placeholder="#7C3AED", required=False, max_length=7,
            default=f"#{cfg['color']:06X}")
        self.btn_label = discord.ui.TextInput(
            label="Entry button text", placeholder="Enter", required=False, max_length=30,
            default=cfg["button_label"])
        self.emoji = discord.ui.TextInput(
            label="Entry button emoji (or none)", placeholder="\U0001F389", required=False, max_length=40,
            default=cfg["button_emoji"] or "none")
        self.footer = discord.ui.TextInput(
            label="Extra footer text", placeholder="Good luck!", required=False, max_length=100,
            default=cfg["footer_text"])
        for item in (self.color, self.btn_label, self.emoji, self.footer):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        raw_color = str(self.color).strip()
        color = hex_color(raw_color) if raw_color else DEFAULT_COLOR
        if color is None:
            return await interaction.response.send_message(
                "That color isn't a valid hex code. Try something like `#7C3AED`.", ephemeral=True)
        emoji = str(self.emoji).strip()
        if emoji.lower() == "none":
            emoji = ""
        elif emoji and not valid_emoji(emoji):
            return await interaction.response.send_message(
                "I couldn't use that emoji. Paste a normal emoji, a server emoji, or type `none`.", ephemeral=True)

        await interaction.response.defer()
        gid = interaction.guild_id
        await self.cog.save_setting(gid, "color", color)
        await self.cog.save_setting(gid, "button_label", str(self.btn_label).strip() or None)
        await self.cog.save_setting(gid, "button_emoji", emoji if (emoji or str(self.emoji).strip()) else None)
        await self.cog.save_setting(gid, "footer_text", str(self.footer).strip() or None)
        await self.cog.refresh_panel(interaction)


class MessagesModal(discord.ui.Modal, title="Messages"):
    def __init__(self, cog, cfg):
        super().__init__()
        self.cog = cog
        self.win = discord.ui.TextInput(
            label="Winner message", style=discord.TextStyle.paragraph, required=False, max_length=1000,
            placeholder="{winners} {prize} {host} {link} {server}", default=cfg["win_message"])
        self.none = discord.ui.TextInput(
            label="No-winner message", style=discord.TextStyle.paragraph, required=False, max_length=500,
            placeholder="{prize} {server}", default=cfg["no_winner_message"])
        self.add_item(self.win)
        self.add_item(self.none)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        gid = interaction.guild_id
        win, none = str(self.win).strip(), str(self.none).strip()
        await self.cog.save_setting(gid, "win_message", None if win in ("", DEFAULT_WIN) else win)
        await self.cog.save_setting(gid, "no_winner_message", None if none in ("", DEFAULT_NONE) else none)
        await self.cog.refresh_panel(interaction)


class SettingsView(discord.ui.View):
    def __init__(self, cog, cfg):
        super().__init__(timeout=300)
        self.cog = cog
        self.add_item(RolePicker(cog, "ping_role_id", "Ping role (pinged for new giveaways)", cfg["ping_role_id"], 0))
        self.add_item(RolePicker(cog, "manager_role_id", "Manager role (can run giveaways)", cfg["manager_role_id"], 1))
        self.add_item(RolePicker(cog, "blacklist_role_id", "Blocked role (can't enter)", cfg["blacklist_role_id"], 2))
        on = cfg["dm_winners"]
        self.dm_toggle.label = f"Winner DMs: {'On' if on else 'Off'}"
        self.dm_toggle.style = discord.ButtonStyle.success if on else discord.ButtonStyle.secondary

    @discord.ui.button(label="Appearance", style=discord.ButtonStyle.primary, row=3)
    async def appearance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AppearanceModal(self.cog, await self.cog.settings(interaction.guild_id)))

    @discord.ui.button(label="Messages", style=discord.ButtonStyle.primary, row=3)
    async def messages(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MessagesModal(self.cog, await self.cog.settings(interaction.guild_id)))

    @discord.ui.button(label="Winner DMs", row=3)
    async def dm_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        cfg = await self.cog.settings(interaction.guild_id)
        await self.cog.save_setting(interaction.guild_id, "dm_winners", not cfg["dm_winners"])
        await self.cog.refresh_panel(interaction)

    @discord.ui.button(label="Reset", style=discord.ButtonStyle.danger, row=3)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.cog.db.reset_settings(interaction.guild_id)
        self.cog._cfg.pop(interaction.guild_id, None)
        await self.cog.refresh_panel(interaction)


# ====================================================================== the cog
@app_commands.guild_only()
class Giveaways(commands.GroupCog, group_name="giveaway",
                group_description="Create and manage giveaways"):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.db = bot.db
        self.schedule = {}   # giveaway id -> end time; held in memory so the database isn't polled
        self._ending = set()
        self._refresh = {}
        self._tasks = set()
        self._cfg = {}       # guild id -> (expiry, settings)

    # ---------------------------------------------------------------- lifecycle
    async def cog_load(self):
        self.bot.add_view(EntryView(self))
        for row in await self.db.active_schedule():
            self.schedule[row["id"]] = row["ends_at"]
        self.watcher.start()

    async def cog_unload(self):
        self.watcher.cancel()

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.guild_permissions.manage_guild:
            return True
        name = interaction.command.name if interaction.command else ""
        if name != "settings" and await self.is_manager(interaction):
            return True
        extra = "" if name == "settings" else " or the giveaway manager role"
        raise app_commands.CheckFailure(f"You need the Manage Server permission{extra} to use that.")

    async def is_manager(self, interaction):
        if interaction.user.guild_permissions.manage_guild:
            return True
        rid = (await self.settings(interaction.guild_id))["manager_role_id"]
        return bool(rid and interaction.user.get_role(rid))

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

    # ---------------------------------------------------------------- settings
    async def settings(self, guild_id):
        cached = self._cfg.get(guild_id)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        row = await self.db.get_settings(guild_id)
        cfg = dict(DEFAULTS)
        if row:
            for key in DEFAULTS:
                if row[key] is not None:
                    cfg[key] = row[key]
        self._cfg[guild_id] = (time.monotonic() + 60, cfg)
        return cfg

    async def save_setting(self, guild_id, field, value):
        await self.db.set_setting(guild_id, field, value)
        self._cfg.pop(guild_id, None)

    def settings_embed(self, cfg):
        embed = discord.Embed(title="Giveaway settings", color=cfg["color"],
                              description="Use the menus and buttons below. Changes apply straight away.")
        embed.add_field(name="Ping role", value=role_text(cfg["ping_role_id"]))
        embed.add_field(name="Manager role", value=role_text(cfg["manager_role_id"]))
        embed.add_field(name="Blocked role", value=role_text(cfg["blacklist_role_id"]))
        embed.add_field(name="Color", value=f"#{cfg['color']:06X}")
        button = f"{cfg['button_emoji']} {cfg['button_label']}".strip() if cfg["button_emoji"] else cfg["button_label"]
        embed.add_field(name="Entry button", value=button)
        embed.add_field(name="Winner DMs", value="On" if cfg["dm_winners"] else "Off")
        embed.add_field(name="Footer", value=cfg["footer_text"] or "None", inline=False)
        embed.add_field(name="Winner message", value=cfg["win_message"][:300], inline=False)
        embed.add_field(name="No-winner message", value=cfg["no_winner_message"][:300], inline=False)
        return embed

    async def refresh_panel(self, interaction):
        cfg = await self.settings(interaction.guild_id)
        await interaction.edit_original_response(embed=self.settings_embed(cfg), view=SettingsView(self, cfg))

    # ---------------------------------------------------------------- helpers
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

    def build_embed(self, g, count, cfg, state="active"):
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

        shade = {"active": cfg["color"], "ended": ENDED_COLOR, "cancelled": CANCELLED_COLOR}[state]
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
            if g["min_account_days"]:
                lines.append(f"Your account must be at least {g['min_account_days']} days old.")
            if g["min_server_days"]:
                lines.append(f"You must have been in this server for {g['min_server_days']} days.")
            if lines:
                embed.add_field(name="Details", value="\n".join(lines), inline=False)
        if g["image_url"]:
            embed.set_image(url=g["image_url"])
        footer = f"Giveaway #{g['id']}"
        if cfg["footer_text"]:
            footer += f" · {cfg['footer_text']}"
        embed.set_footer(text=footer[:2048])
        embed.timestamp = ends
        return embed

    # ---------------------------------------------------------------- message updates
    def schedule_refresh(self, gid):
        """Coalesce rapid joins into one embed edit to stay clear of rate limits."""
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
        try:
            await msg.edit(embed=self.build_embed(g, count, await self.settings(g["guild_id"])))
        except discord.HTTPException:
            pass

    async def render_final(self, g):
        state = "cancelled" if g["cancelled"] else "ended"
        msg = self.partial(await self.get_channel(g["channel_id"]), g)
        if msg is None:
            return
        count = await self.db.entry_count(g["id"])
        embed = self.build_embed(g, count, await self.settings(g["guild_id"]), state)
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

    # ---------------------------------------------------------------- drawing
    async def pick_winners(self, guild, g, entries, k, exclude):
        """Draw in weighted random order, checking each candidate is still eligible."""
        blocked = (await self.settings(guild.id))["blacklist_role_id"]
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
            if blocked and member.get_role(blocked) is not None:
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

            cfg = await self.settings(g["guild_id"])
            server = guild.name if guild else ""
            values = dict(prize=g["prize"], host=f"<@{g['host_id']}>", link=jump(g), server=server)
            if winners:
                names = ", ".join(f"<@{u}>" for u in winners)
                await self.announce(g, fill(cfg["win_message"], winners=names, **values))
                if cfg["dm_winners"] and guild:
                    for uid in winners:
                        await self.dm_winner(uid, g, guild.name)
            else:
                await self.announce(g, fill(cfg["no_winner_message"], winners="nobody", **values))
            return g
        finally:
            self._ending.discard(gid)

    # ---------------------------------------------------------------- entering
    async def handle_enter(self, interaction):
        await interaction.response.defer(ephemeral=True)
        g = await self.db.get_by_message(interaction.message.id)
        if g is None or g["ended"] or g["cancelled"] or g["ends_at"] <= utcnow():
            return await interaction.followup.send("This giveaway is no longer taking entries.", ephemeral=True)

        member = interaction.user
        cfg = await self.settings(g["guild_id"])
        if cfg["blacklist_role_id"] and member.get_role(cfg["blacklist_role_id"]) is not None:
            return await interaction.followup.send("You can't enter giveaways in this server.", ephemeral=True)
        if g["required_role_id"] and member.get_role(g["required_role_id"]) is None:
            return await interaction.followup.send(
                f"You need the <@&{g['required_role_id']}> role to enter this one.", ephemeral=True)
        if g["min_account_days"] and (utcnow() - member.created_at).days < g["min_account_days"]:
            return await interaction.followup.send(
                f"Your Discord account needs to be at least {g['min_account_days']} days old to enter.", ephemeral=True)
        if g["min_server_days"]:
            joined = member.joined_at
            if joined is None or (utcnow() - joined).days < g["min_server_days"]:
                return await interaction.followup.send(
                    f"You need to have been in this server for {g['min_server_days']} days to enter.", ephemeral=True)

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

    # ---------------------------------------------------------------- autocomplete
    async def _choices(self, interaction, current, mode):
        try:
            if not await self.is_manager(interaction):
                return []
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

    # ---------------------------------------------------------------- commands
    @app_commands.command(name="start", description="Start a giveaway")
    @app_commands.describe(
        prize="What's being given away",
        duration="How long it runs, e.g. 30m, 2h, 1d12h",
        winners="How many winners (default 1)",
        channel="Where to post it (default: this channel)",
        required_role="Only members with this role can enter",
        bonus_role="Members with this role get extra entries",
        bonus_entries="How many extra entries the bonus role gets (default 1)",
        min_account_days="Minimum Discord account age in days",
        min_server_days="Minimum days a member has been in this server",
        description="Extra text shown on the giveaway",
        image="Link to an image to show on the giveaway (https)",
        host="Show someone else as the host")
    async def gw_start(self, interaction: discord.Interaction,
                       prize: app_commands.Range[str, 1, 100],
                       duration: str,
                       winners: app_commands.Range[int, 1, 20] = 1,
                       channel: Optional[discord.TextChannel] = None,
                       required_role: Optional[discord.Role] = None,
                       bonus_role: Optional[discord.Role] = None,
                       bonus_entries: app_commands.Range[int, 1, 50] = 1,
                       min_account_days: Optional[app_commands.Range[int, 1, 3650]] = None,
                       min_server_days: Optional[app_commands.Range[int, 1, 3650]] = None,
                       description: Optional[app_commands.Range[str, 1, 500]] = None,
                       image: Optional[app_commands.Range[str, 1, 400]] = None,
                       host: Optional[discord.Member] = None):
        seconds = parse_duration(duration)
        if seconds is None:
            return await self.say(interaction, "I couldn't read that duration. Try something like `30m`, `2h` or `1d12h`.")
        if seconds < MIN_SECONDS:
            return await self.say(interaction, "Giveaways need to run for at least 30 seconds.")
        if seconds > MAX_SECONDS:
            return await self.say(interaction, "Giveaways can run for 90 days at most.")
        if image and not image.lower().startswith("https://"):
            return await self.say(interaction, "The image needs to be a link starting with `https://`.")

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
            guild_id=interaction.guild_id, channel_id=channel.id,
            host_id=(host or interaction.user).id,
            prize=prize, description=description, winners=winners,
            ends_at=utcnow() + dt.timedelta(seconds=seconds),
            required_role_id=required_role.id if required_role else None,
            bonus_role_id=bonus_role.id if bonus_role else None,
            bonus_entries=bonus_entries if bonus_role else 0,
            image_url=image, min_account_days=min_account_days or 0, min_server_days=min_server_days or 0)

        cfg = await self.settings(interaction.guild_id)
        ping = role_text(cfg["ping_role_id"]) if cfg["ping_role_id"] else None
        try:
            msg = await channel.send(
                content=ping, embed=self.build_embed(g, 0, cfg),
                view=EntryView(self, cfg["button_label"], cfg["button_emoji"]),
                allowed_mentions=discord.AllowedMentions(roles=True) if ping else None)
        except discord.HTTPException:
            await self.db.delete(g["id"])
            return await self.say(interaction, "I couldn't post that. Check my permissions in the channel, and that the image link works.")

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
        if (await self.settings(g["guild_id"]))["dm_winners"]:
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
        await interaction.response.defer(ephemeral=True)
        rows = await self.db.active(interaction.guild_id)
        if not rows:
            return await interaction.followup.send("There are no running giveaways right now.", ephemeral=True)
        lines = [f"**#{r['id']}** · [{tidy(r['prize'])}]({jump(r)}) · ends {ts(r['ends_at'], 'R')} · "
                 f"{r['entry_count']:,} {'entry' if r['entry_count'] == 1 else 'entries'}" for r in rows]
        cfg = await self.settings(interaction.guild_id)
        embed = discord.Embed(title="Running giveaways", description="\n".join(lines), color=cfg["color"])
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="info", description="Show the details of a giveaway")
    @app_commands.describe(giveaway="Which giveaway")
    async def gw_info(self, interaction: discord.Interaction, giveaway: int):
        g = await self.fetch_owned(interaction, giveaway)
        if g is None:
            return
        await interaction.response.defer(ephemeral=True)
        count = await self.db.entry_count(giveaway)
        cfg = await self.settings(g["guild_id"])
        status = "Cancelled" if g["cancelled"] else "Ended" if g["ended"] else "Running"
        embed = discord.Embed(title=g["prize"], color=cfg["color"], url=jump(g) if g["message_id"] else None)
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
        if g["min_account_days"]:
            embed.add_field(name="Min account age", value=f"{g['min_account_days']} days")
        if g["min_server_days"]:
            embed.add_field(name="Min time in server", value=f"{g['min_server_days']} days")
        if g["winner_ids"]:
            embed.add_field(name="Winners drawn", value=", ".join(f"<@{u}>" for u in g["winner_ids"])[:1000],
                            inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="settings", description="Open the settings panel for this server")
    async def gw_settings(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cfg = await self.settings(interaction.guild_id)
        await interaction.followup.send(embed=self.settings_embed(cfg), view=SettingsView(self, cfg), ephemeral=True)

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
