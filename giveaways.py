import asyncio
import datetime as dt
import logging
import re
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import parse_duration, public_url, weighted_order

log = logging.getLogger("giftly.giveaways")

DEFAULT_COLOR = 0x7C3AED
ENDED_COLOR = 0x4B5563
CANCELLED_COLOR = 0x991B1B
MIN_SECONDS = 30
MAX_SECONDS = 90 * 86400
DEFAULT_WIN = "Congratulations {winners}! You won **{prize}**."
DEFAULT_NONE = "Nobody entered **{prize}** with a valid entry, so there's no winner."
WINNER_MENTIONS = discord.AllowedMentions(users=True, roles=False, everyone=False)

DEFAULT_DM = "You won **{prize}** in **{server}**.\n[Jump to the giveaway]({link})"
ADMIN_ONLY = {"settings", "bonus-add", "bonus-remove"}
PAGES = {"roles": "Roles", "look": "Look and feel", "messages": "Messages", "defaults": "Defaults and rules"}

DEFAULTS = {
    "color": DEFAULT_COLOR, "ping_role_id": None, "manager_role_id": None, "blacklist_role_id": None,
    "bypass_role_id": None, "button_label": "Enter", "button_emoji": "\U0001F389", "footer_text": None,
    "author_text": "Giveaway", "thumbnail_url": None, "show_entries": True,
    "win_message": DEFAULT_WIN, "no_winner_message": DEFAULT_NONE, "dm_message": DEFAULT_DM,
    "dm_winners": True, "default_channel_id": None, "default_duration": None, "default_winners": 1,
    "stack_bonuses": True, "claim_minutes": 0, "show_branding": True,
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


class ClaimView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Claim prize", emoji="\U0001F381", style=discord.ButtonStyle.success,
                       custom_id="giftly:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_claim(interaction)


# ====================================================================== settings panel
class PageSelect(discord.ui.Select):
    def __init__(self, cog, page):
        super().__init__(placeholder="Settings page", row=0, options=[
            discord.SelectOption(label=label, value=key, default=key == page) for key, label in PAGES.items()])
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.cog.refresh_panel(interaction, self.values[0])


class RolePicker(discord.ui.RoleSelect):
    def __init__(self, cog, page, field, placeholder, current, row):
        defaults = ([discord.SelectDefaultValue(id=current, type=discord.SelectDefaultValueType.role)]
                    if current else [])
        super().__init__(placeholder=placeholder, min_values=0, max_values=1,
                         default_values=defaults, row=row)
        self.cog, self.page, self.field = cog, page, field

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        value = self.values[0].id if self.values else None
        await self.cog.save_setting(interaction.guild_id, self.field, value)
        await self.cog.refresh_panel(interaction, self.page)


class ChannelPicker(discord.ui.ChannelSelect):
    def __init__(self, cog, page, current, row):
        defaults = ([discord.SelectDefaultValue(id=current, type=discord.SelectDefaultValueType.channel)]
                    if current else [])
        super().__init__(placeholder="Default channel for new giveaways", min_values=0, max_values=1,
                         channel_types=[discord.ChannelType.text], default_values=defaults, row=row)
        self.cog, self.page = cog, page

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        value = self.values[0].id if self.values else None
        await self.cog.save_setting(interaction.guild_id, "default_channel_id", value)
        await self.cog.refresh_panel(interaction, self.page)


class ActionButton(discord.ui.Button):
    def __init__(self, label, handler, style=discord.ButtonStyle.secondary, row=1):
        super().__init__(label=label, style=style, row=row)
        self.handler = handler

    async def callback(self, interaction: discord.Interaction):
        await self.handler(interaction)


class AppearanceModal(discord.ui.Modal, title="Colors and button"):
    def __init__(self, cog, cfg, page):
        super().__init__()
        self.cog, self.page = cog, page
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
        raw_emoji = emoji
        if emoji.lower() == "none":
            emoji = ""
        elif emoji and not valid_emoji(emoji):
            return await interaction.response.send_message(
                "I couldn't use that emoji. Paste a normal emoji, a server emoji, or type `none`.", ephemeral=True)

        await interaction.response.defer()
        gid = interaction.guild_id
        await self.cog.save_setting(gid, "color", color)
        await self.cog.save_setting(gid, "button_label", str(self.btn_label).strip() or None)
        await self.cog.save_setting(gid, "button_emoji", emoji if raw_emoji else None)
        await self.cog.save_setting(gid, "footer_text", str(self.footer).strip() or None)
        await self.cog.refresh_panel(interaction, self.page)


class EmbedModal(discord.ui.Modal, title="Label and image"):
    def __init__(self, cog, cfg, page):
        super().__init__()
        self.cog, self.page = cog, page
        self.author = discord.ui.TextInput(
            label="Label above the title", placeholder="Giveaway", required=False, max_length=30,
            default=cfg["author_text"])
        self.thumb = discord.ui.TextInput(
            label="Thumbnail image link (https)", placeholder="https://...", required=False, max_length=300,
            default=cfg["thumbnail_url"])
        self.add_item(self.author)
        self.add_item(self.thumb)

    async def on_submit(self, interaction: discord.Interaction):
        thumb = str(self.thumb).strip()
        if thumb and not thumb.lower().startswith("https://"):
            return await interaction.response.send_message(
                "The thumbnail needs to be a link starting with `https://`.", ephemeral=True)
        await interaction.response.defer()
        author = str(self.author).strip()
        gid = interaction.guild_id
        await self.cog.save_setting(gid, "author_text", None if author in ("", "Giveaway") else author)
        await self.cog.save_setting(gid, "thumbnail_url", thumb or None)
        await self.cog.refresh_panel(interaction, self.page)


class MessagesModal(discord.ui.Modal, title="Messages"):
    def __init__(self, cog, cfg, page):
        super().__init__()
        self.cog, self.page = cog, page
        self.win = discord.ui.TextInput(
            label="Winner message", style=discord.TextStyle.paragraph, required=False, max_length=1000,
            placeholder="{winners} {prize} {host} {link} {server}", default=cfg["win_message"])
        self.none = discord.ui.TextInput(
            label="No-winner message", style=discord.TextStyle.paragraph, required=False, max_length=500,
            placeholder="{prize} {server}", default=cfg["no_winner_message"])
        self.dm = discord.ui.TextInput(
            label="Winner DM", style=discord.TextStyle.paragraph, required=False, max_length=1000,
            placeholder="{prize} {server} {link} {host}", default=cfg["dm_message"])
        for item in (self.win, self.none, self.dm):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        gid = interaction.guild_id
        for field, box, default in (("win_message", self.win, DEFAULT_WIN),
                                    ("no_winner_message", self.none, DEFAULT_NONE),
                                    ("dm_message", self.dm, DEFAULT_DM)):
            text = str(box).strip()
            await self.cog.save_setting(gid, field, None if text in ("", default) else text)
        await self.cog.refresh_panel(interaction, self.page)


class DefaultsModal(discord.ui.Modal, title="Default time and winners"):
    def __init__(self, cog, cfg, page):
        super().__init__()
        self.cog, self.page = cog, page
        self.duration = discord.ui.TextInput(
            label="Default duration", placeholder="e.g. 24h or 3d", required=False, max_length=20,
            default=cfg["default_duration"])
        self.winners = discord.ui.TextInput(
            label="Default number of winners", placeholder="1", required=False, max_length=2,
            default=str(cfg["default_winners"]))
        self.claim = discord.ui.TextInput(
            label="Claim time in minutes (0 = off)", placeholder="0", required=False, max_length=4,
            default=str(cfg["claim_minutes"]))
        self.add_item(self.duration)
        self.add_item(self.winners)
        self.add_item(self.claim)

    async def on_submit(self, interaction: discord.Interaction):
        duration = str(self.duration).strip()
        if duration:
            seconds = parse_duration(duration)
            if seconds is None or not MIN_SECONDS <= seconds <= MAX_SECONDS:
                return await interaction.response.send_message(
                    "Use a duration like `24h` or `3d` between 30 seconds and 90 days.", ephemeral=True)
        raw = str(self.winners).strip()
        winners = None
        if raw:
            if not raw.isdigit() or not 1 <= int(raw) <= 20:
                return await interaction.response.send_message(
                    "Winners needs to be a number from 1 to 20.", ephemeral=True)
            winners = int(raw)
        claim_raw = str(self.claim).strip() or "0"
        if not claim_raw.isdigit() or int(claim_raw) > 1440:
            return await interaction.response.send_message(
                "Claim time must be a number of minutes from 0 to 1440.", ephemeral=True)
        await interaction.response.defer()
        await self.cog.save_setting(interaction.guild_id, "claim_minutes", int(claim_raw))
        await self.cog.save_setting(interaction.guild_id, "default_duration", duration or None)
        await self.cog.save_setting(interaction.guild_id, "default_winners", winners)
        await self.cog.refresh_panel(interaction, self.page)


class SettingsView(discord.ui.View):
    def __init__(self, cog, cfg, page="roles"):
        super().__init__(timeout=300)
        self.cog, self.cfg, self.page = cog, cfg, page
        self.add_item(PageSelect(cog, page))
        primary, danger = discord.ButtonStyle.primary, discord.ButtonStyle.danger
        if page == "roles":
            roles = (("ping_role_id", "Ping role (pinged for new giveaways)"),
                     ("manager_role_id", "Manager role (can run giveaways)"),
                     ("blacklist_role_id", "Blocked role (can't enter or win)"),
                     ("bypass_role_id", "Bypass role (skips entry requirements)"))
            for row, (field, text) in enumerate(roles, start=1):
                self.add_item(RolePicker(cog, page, field, text, cfg[field], row))
        elif page == "look":
            self.add_item(ActionButton("Colors and button", self.open(AppearanceModal), primary))
            self.add_item(ActionButton("Label and image", self.open(EmbedModal), primary))
            self.add_item(ActionButton(f"Entry count: {'shown' if cfg['show_entries'] else 'hidden'}",
                                       self.toggler("show_entries")))
            self.add_item(ActionButton(f"Powered-by line: {'shown' if cfg['show_branding'] else 'hidden'}",
                                       self.toggler("show_branding")))
        elif page == "messages":
            self.add_item(ActionButton("Edit messages", self.open(MessagesModal), primary))
            self.add_item(ActionButton(f"Winner DMs: {'on' if cfg['dm_winners'] else 'off'}",
                                       self.toggler("dm_winners")))
        else:
            self.add_item(ChannelPicker(cog, page, cfg["default_channel_id"], 1))
            self.add_item(ActionButton("Default time and winners", self.open(DefaultsModal), primary, row=2))
            self.add_item(ActionButton(f"Stack bonuses: {'on' if cfg['stack_bonuses'] else 'off'}",
                                       self.toggler("stack_bonuses"), row=2))
            self.add_item(ActionButton("Reset everything", self.reset, danger, row=2))

    def open(self, modal_cls):
        async def handler(interaction):
            await interaction.response.send_modal(modal_cls(self.cog, self.cfg, self.page))
        return handler

    def toggler(self, field):
        async def handler(interaction):
            await interaction.response.defer()
            await self.cog.save_setting(interaction.guild_id, field, not self.cfg[field])
            await self.cog.refresh_panel(interaction, self.page)
        return handler

    async def reset(self, interaction):
        await interaction.response.defer()
        await self.cog.db.reset_settings(interaction.guild_id)
        self.cog._cfg.pop(interaction.guild_id, None)
        await self.cog.refresh_panel(interaction, self.page)


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
        self.claims = {}     # giveaway id -> claim deadline

    # ---------------------------------------------------------------- lifecycle
    async def cog_load(self):
        self.bot.add_view(EntryView(self))
        self.bot.add_view(ClaimView(self))
        for row in await self.db.active_schedule():
            self.schedule[row["id"]] = row["ends_at"]
        for row in await self.db.active_claims():
            self.claims[row["id"]] = row["claim_deadline"]
        self.watcher.start()

    async def cog_unload(self):
        self.watcher.cancel()

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.guild_permissions.manage_guild:
            return True
        name = interaction.command.name if interaction.command else ""
        if name not in ADMIN_ONLY and await self.is_manager(interaction):
            return True
        extra = "" if name in ADMIN_ONLY else " or the giveaway manager role"
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
        for gid, deadline in list(self.claims.items()):
            if deadline <= now and gid not in self._ending:
                self.spawn(self._expire_safely(gid))

    @watcher.before_loop
    async def _before_watcher(self):
        await self.bot.wait_until_ready()

    async def _expire_safely(self, gid):
        try:
            await self.expire_claim(gid)
        except Exception:
            log.exception("Failed to process claim window for giveaway %s, retrying in a minute", gid)
            self.claims[gid] = utcnow() + dt.timedelta(seconds=60)

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
        cfg["bonus"] = await self.db.bonus_list(guild_id)
        self._cfg[guild_id] = (time.monotonic() + 60, cfg)
        return cfg

    async def save_setting(self, guild_id, field, value):
        await self.db.set_setting(guild_id, field, value)
        self._cfg.pop(guild_id, None)

    def settings_embed(self, cfg):
        embed = discord.Embed(
            title="Giveaway settings", color=cfg["color"],
            description="Pick a page from the menu, then change what you like. Changes apply straight away.")
        button = f"{cfg['button_emoji']} {cfg['button_label']}".strip() if cfg["button_emoji"] else cfg["button_label"]
        for name, value in (
                ("Ping role", role_text(cfg["ping_role_id"])),
                ("Manager role", role_text(cfg["manager_role_id"])),
                ("Blocked role", role_text(cfg["blacklist_role_id"])),
                ("Bypass role", role_text(cfg["bypass_role_id"])),
                ("Color", f"#{cfg['color']:06X}"),
                ("Entry button", button),
                ("Label", cfg["author_text"]),
                ("Thumbnail", "Set" if cfg["thumbnail_url"] else "None"),
                ("Entry count", "Shown" if cfg["show_entries"] else "Hidden"),
                ("Powered-by line", "Shown" if cfg["show_branding"] else "Hidden"),
                ("Default channel", f"<#{cfg['default_channel_id']}>" if cfg["default_channel_id"] else "Where you run it"),
                ("Default time", cfg["default_duration"] or "Not set"),
                ("Default winners", str(cfg["default_winners"])),
                ("Claim window", f"{cfg['claim_minutes']} min" if cfg["claim_minutes"] else "Off"),
                ("Winner DMs", "On" if cfg["dm_winners"] else "Off"),
                ("Stack bonuses", "On" if cfg["stack_bonuses"] else "Off")):
            embed.add_field(name=name, value=value)
        bonus = ", ".join(f"<@&{rid}> +{n}" for rid, n in cfg["bonus"].items())
        embed.add_field(name="Bonus roles", value=bonus or "None. Add one with `/giveaway bonus-add`.", inline=False)
        embed.add_field(name="Footer", value=cfg["footer_text"] or "None", inline=False)
        embed.add_field(name="Winner message", value=cfg["win_message"][:200], inline=False)
        embed.add_field(name="No-winner message", value=cfg["no_winner_message"][:200], inline=False)
        embed.add_field(name="Winner DM", value=cfg["dm_message"][:200], inline=False)
        return embed

    async def refresh_panel(self, interaction, page="roles"):
        cfg = await self.settings(interaction.guild_id)
        await interaction.edit_original_response(embed=self.settings_embed(cfg), view=SettingsView(self, cfg, page))

    def entry_weight(self, member, g, cfg):
        earned = [n for rid, n in cfg["bonus"].items() if member.get_role(rid) is not None]
        extra = sum(earned) if cfg["stack_bonuses"] else max(earned, default=0)
        if g["bonus_role_id"] and member.get_role(g["bonus_role_id"]) is not None:
            extra += g["bonus_entries"]
        return 1 + extra

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
        label = cfg["author_text"]
        site = public_url()
        embed.set_author(name={"active": label, "ended": f"{label} ended", "cancelled": f"{label} cancelled"}[state],
                         url=f"{site}/invite" if site else None)
        embed.add_field(name="Hosted by", value=f"<@{g['host_id']}>")
        if state == "ended":
            winners = ", ".join(f"<@{u}>" for u in g["winner_ids"]) or "No valid entries"
            embed.add_field(name="Winners", value=winners[:1000])
        else:
            embed.add_field(name="Winners", value=str(g["winners"]))
        if cfg["show_entries"]:
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
            if cfg["bonus"]:
                lines.append("Bonus entries: " + ", ".join(f"<@&{rid}> +{n}" for rid, n in cfg["bonus"].items()))
            if lines:
                embed.add_field(name="Details", value="\n".join(lines)[:1000], inline=False)
        if g["image_url"]:
            embed.set_image(url=g["image_url"])
        if cfg["thumbnail_url"]:
            embed.set_thumbnail(url=cfg["thumbnail_url"])
        footer = [f"Giveaway #{g['id']}"]
        if cfg["footer_text"]:
            footer.append(cfg["footer_text"])
        if cfg["show_branding"]:
            footer.append("Powered by Dormexed Productions")
        embed.set_footer(text=" · ".join(footer)[:2048])
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

    async def announce(self, g, text, view=None):
        channel = await self.get_channel(g["channel_id"])
        if channel is None:
            return None
        options = {"allowed_mentions": WINNER_MENTIONS}
        if view is not None:
            options["view"] = view
        msg = self.partial(channel, g)
        try:
            if msg is not None:
                return await msg.reply(text, **options)
            return await channel.send(text, **options)
        except discord.HTTPException:
            try:
                return await channel.send(text, **options)
            except discord.HTTPException:
                return None

    async def dm_winner(self, uid, g, guild_name):
        try:
            cfg = await self.settings(g["guild_id"])
            text = fill(cfg["dm_message"], prize=g["prize"], server=guild_name,
                        host=f"<@{g['host_id']}>", link=jump(g), winners=f"<@{uid}>")
            user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            await user.send(embed=discord.Embed(title="You won!", description=text, color=cfg["color"]))
        except discord.HTTPException:
            pass

    # ---------------------------------------------------------------- drawing
    async def pick_winners(self, guild, g, entries, k, exclude):
        """Draw in weighted random order, checking each candidate is still eligible."""
        cfg = await self.settings(guild.id)
        blocked, bypass = cfg["blacklist_role_id"], cfg["bypass_role_id"]
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
            if blocked and member.get_role(blocked) is not None:
                continue
            skips_rules = bool(bypass and member.get_role(bypass) is not None)
            if g["required_role_id"] and not skips_rules and member.get_role(g["required_role_id"]) is None:
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
                await self.open_claim(g, fill(cfg["win_message"], winners=names, **values), cfg)
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

        if not (cfg["bypass_role_id"] and member.get_role(cfg["bypass_role_id"]) is not None):
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

        weight = self.entry_weight(member, g, cfg)
        if await self.db.add_entry(g["id"], member.id, weight):
            self.schedule_refresh(g["id"])
            text = "You're in. Good luck!"
            if weight > 1:
                text += f" Your roles give you {weight} entries."
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.followup.send(
                "You're already entered. Want to leave?",
                view=LeaveView(self, g["id"], member.id), ephemeral=True)

    # ---------------------------------------------------------------- claiming
    @staticmethod
    def drawn(g):
        return set(g["winner_ids"]) | set(g["excluded_ids"])

    async def open_claim(self, g, text, cfg):
        """Announce winners. With a claim window set, attach the button and start the clock."""
        minutes = cfg["claim_minutes"]
        if not minutes:
            return await self.announce(g, text)
        deadline = utcnow() + dt.timedelta(minutes=minutes)
        text += f"\nPress **Claim prize** before {ts(deadline, 't')} or a new winner will be drawn."
        msg = await self.announce(g, text, view=ClaimView(self))
        if msg is not None:
            await self.db.start_claim(g["id"], deadline, msg.id)
            self.claims[g["id"]] = deadline
        return msg

    async def handle_claim(self, interaction):
        await interaction.response.defer(ephemeral=True)
        g = await self.db.get_by_claim_message(interaction.message.id)
        if g is None or g["claim_deadline"] is None or g["claim_deadline"] <= utcnow():
            return await interaction.followup.send("The claim window for this giveaway has closed.", ephemeral=True)
        uid = interaction.user.id
        if uid not in g["winner_ids"]:
            return await interaction.followup.send("Only the winners can claim this prize.", ephemeral=True)
        g = await self.db.add_claim(g["id"], uid)
        if g is None:
            return await interaction.followup.send("You've already claimed this one.", ephemeral=True)
        await interaction.followup.send("Claimed! The host will be in touch.", ephemeral=True)
        if all(u in g["claimed_ids"] for u in g["winner_ids"]):
            await self.db.clear_claim(g["id"])
            self.claims.pop(g["id"], None)
            try:
                await interaction.message.edit(view=closed_view("All claimed"))
            except discord.HTTPException:
                pass

    async def expire_claim(self, gid):
        """The claim window ran out: redraw for anyone who didn't claim."""
        if gid in self._ending:
            return
        self._ending.add(gid)
        try:
            self.claims.pop(gid, None)
            g = await self.db.get(gid)
            if g is None or g["claim_deadline"] is None:
                return
            channel = await self.get_channel(g["channel_id"])
            if channel is not None and g["claim_message_id"]:
                try:
                    await channel.get_partial_message(g["claim_message_id"]).edit(view=closed_view("Expired"))
                except discord.HTTPException:
                    pass
            guild = self.bot.get_guild(g["guild_id"])
            missing = [u for u in g["winner_ids"] if u not in g["claimed_ids"]]
            if not missing or guild is None:
                await self.db.clear_claim(gid)
                return

            entries = await self.db.entries(gid)
            await self.db.add_excluded(gid, missing)
            g = await self.db.get(gid)
            replacements = await self.pick_winners(guild, g, entries, len(missing), self.drawn(g))
            kept = [u for u in g["winner_ids"] if u in g["claimed_ids"]]
            await self.db.set_winners(gid, kept + replacements)
            g = await self.db.clear_claim(gid)
            await self.render_final(g)

            lost = ", ".join(f"<@{u}>" for u in missing)
            if replacements:
                names = ", ".join(f"<@{u}>" for u in replacements)
                cfg = await self.settings(g["guild_id"])
                await self.open_claim(
                    g, f"{lost} didn't claim in time. New {'winner' if len(replacements) == 1 else 'winners'} "
                       f"for **{g['prize']}**: {names}. Congratulations!", cfg)
                if cfg["dm_winners"]:
                    for uid in replacements:
                        await self.dm_winner(uid, g, guild.name)
            else:
                await self.announce(g, f"{lost} didn't claim **{g['prize']}** in time, and there's nobody left to draw from.")
        finally:
            self._ending.discard(gid)

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
                       duration: Optional[str] = None,
                       winners: Optional[app_commands.Range[int, 1, 20]] = None,
                       channel: Optional[discord.TextChannel] = None,
                       required_role: Optional[discord.Role] = None,
                       bonus_role: Optional[discord.Role] = None,
                       bonus_entries: app_commands.Range[int, 1, 50] = 1,
                       min_account_days: Optional[app_commands.Range[int, 1, 3650]] = None,
                       min_server_days: Optional[app_commands.Range[int, 1, 3650]] = None,
                       description: Optional[app_commands.Range[str, 1, 500]] = None,
                       image: Optional[app_commands.Range[str, 1, 400]] = None,
                       host: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        cfg = await self.settings(interaction.guild_id)
        duration = duration or cfg["default_duration"]
        if not duration:
            return await self.say(interaction, "Add a duration, or set a default one in `/giveaway settings`.")
        winners = winners or cfg["default_winners"]
        seconds = parse_duration(duration)
        if seconds is None:
            return await self.say(interaction, "I couldn't read that duration. Try something like `30m`, `2h` or `1d12h`.")
        if seconds < MIN_SECONDS:
            return await self.say(interaction, "Giveaways need to run for at least 30 seconds.")
        if seconds > MAX_SECONDS:
            return await self.say(interaction, "Giveaways can run for 90 days at most.")
        if image and not image.lower().startswith("https://"):
            return await self.say(interaction, "The image needs to be a link starting with `https://`.")

        if channel is None and cfg["default_channel_id"]:
            channel = interaction.guild.get_channel(cfg["default_channel_id"])
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await self.say(interaction, "Pick a regular text channel for this giveaway.")
        perms = channel.permissions_for(interaction.guild.me)
        lacking = [name for name, ok in (("View Channel", perms.view_channel),
                                         ("Send Messages", perms.send_messages),
                                         ("Embed Links", perms.embed_links)) if not ok]
        if lacking:
            return await self.say(interaction, f"I'm missing these permissions in {channel.mention}: {', '.join(lacking)}.")

        g = await self.db.create(
            guild_id=interaction.guild_id, channel_id=channel.id,
            host_id=(host or interaction.user).id,
            prize=prize, description=description, winners=winners,
            ends_at=utcnow() + dt.timedelta(seconds=seconds),
            required_role_id=required_role.id if required_role else None,
            bonus_role_id=bonus_role.id if bonus_role else None,
            bonus_entries=bonus_entries if bonus_role else 0,
            image_url=image, min_account_days=min_account_days or 0, min_server_days=min_server_days or 0)

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
        picked = await self.pick_winners(guild, g, entries, 1 if replace else count, self.drawn(g))
        if not picked:
            return await interaction.followup.send("There are no other eligible entries left to draw from.", ephemeral=True)

        kept = [u for u in g["winner_ids"] if not replace or u != replace.id]
        g = await self.db.set_winners(giveaway, kept + picked)
        if replace:
            g = await self.db.add_excluded(giveaway, [replace.id])
        await self.render_final(g)
        names = ", ".join(f"<@{u}>" for u in picked)
        await self.open_claim(
            g, f"New {'winner' if len(picked) == 1 else 'winners'} for **{g['prize']}**: {names}. Congratulations!",
            await self.settings(g["guild_id"]))
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
        await interaction.followup.send(embed=self.settings_embed(cfg), view=SettingsView(self, cfg, "roles"), ephemeral=True)

    @app_commands.command(name="bonus-add", description="Give a role bonus entries in every giveaway")
    @app_commands.describe(role="The role that earns bonus entries", entries="How many extra entries it adds")
    async def gw_bonus_add(self, interaction: discord.Interaction, role: discord.Role,
                           entries: app_commands.Range[int, 1, 50]):
        await interaction.response.defer(ephemeral=True)
        cfg = await self.settings(interaction.guild_id)
        if role.id not in cfg["bonus"] and len(cfg["bonus"]) >= 10:
            return await interaction.followup.send("You can have up to 10 bonus roles. Remove one first.", ephemeral=True)
        await self.db.bonus_set(interaction.guild_id, role.id, entries)
        self._cfg.pop(interaction.guild_id, None)
        await interaction.followup.send(
            f"{role.mention} now earns +{entries} {'entry' if entries == 1 else 'entries'} in every giveaway.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="bonus-remove", description="Remove a bonus entries role")
    @app_commands.describe(role="The role to remove")
    async def gw_bonus_remove(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        removed = await self.db.bonus_remove(interaction.guild_id, role.id)
        self._cfg.pop(interaction.guild_id, None)
        await interaction.followup.send(
            f"{role.mention} no longer earns bonus entries." if removed else f"{role.mention} wasn't a bonus role.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

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
