"""Posts an announcement to a Discord channel whenever a new version of the bot goes live.

Render tells us which Git commit it deployed (RENDER_GIT_COMMIT). If that commit is
different from the last one we announced, we ask GitHub what changed and post it.
"""
import datetime as dt
import logging
import os
import re

import aiohttp
import discord

log = logging.getLogger("giftly.updates")

CHANNEL_ID = int(os.environ.get("UPDATES_CHANNEL_ID", "1558428213598883850"))
API = "https://api.github.com"

# GitHub's automatic commit messages say nothing useful, so we describe the changed files instead.
GENERIC = re.compile(
    r"^(add files via upload|upload files?|initial commit|(update|create|delete|rename|upload)\s+\S+\.\w{1,5})$",
    re.I)

AREAS = {
    "giveaways.py": "giveaway commands",
    "database.py": "database",
    "bot.py": "core bot",
    "updates.py": "update announcements",
    "utils.py": "internals",
    "site.html": "website",
    "terms.html": "terms of service",
    "logo.png": "logo",
    "requirements.txt": "dependencies",
}


async def _github(session, path, token):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "giftly-bot"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with session.get(f"{API}{path}", headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            log.info("GitHub returned %s for %s", resp.status, path)
    except aiohttp.ClientError as exc:
        log.warning("GitHub request failed: %s", exc)
    return None


async def _collect(slug, last, sha, token):
    """Return (commit messages, changed file names) between the last announced commit and this one."""
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if last:
            data = await _github(session, f"/repos/{slug}/compare/{last}...{sha}", token)
            if data:
                return ([c["commit"]["message"] for c in data.get("commits", [])],
                        [f["filename"] for f in data.get("files", [])])
        data = await _github(session, f"/repos/{slug}/commits/{sha}", token)
        if data:
            return [data["commit"]["message"]], [f["filename"] for f in data.get("files", [])]
    return [], []


def build_embed(sha, messages, files):
    lines = []
    for message in messages:
        first = message.strip().splitlines()[0].strip() if message.strip() else ""
        if first and not GENERIC.match(first) and first not in lines:
            lines.append(first)
    areas = []
    for path in files:
        name = AREAS.get(path.split("/")[-1], path)
        if name not in areas:
            areas.append(name)

    embed = discord.Embed(title="Giftly was just updated", color=0x7C3AED,
                          timestamp=dt.datetime.now(dt.timezone.utc))
    if lines:
        embed.description = "\n".join(f"- {line}" for line in lines[:10])[:3500]
        if areas:
            embed.add_field(name="Changed", value=", ".join(areas)[:1000], inline=False)
    elif areas:
        embed.description = "Changes to " + ", ".join(areas) + "."
    else:
        embed.description = "A new version is live."
    embed.set_footer(text=f"Version {sha[:7]}")
    return embed


async def announce_update(bot):
    sha = os.environ.get("RENDER_GIT_COMMIT")
    slug = os.environ.get("RENDER_GIT_REPO_SLUG")
    if not sha or not slug:
        log.info("Update announcement skipped: RENDER_GIT_COMMIT / RENDER_GIT_REPO_SLUG not set (not a Git deploy on Render?)")
        return
    try:
        last = await bot.db.meta_get("last_announced_commit")
        if last == sha:
            log.info("Update %s was already announced, nothing to post", sha[:7])
            return
        log.info("New version %s detected, posting update announcement", sha[:7])
        messages, files = await _collect(slug, last, sha, os.environ.get("GITHUB_TOKEN"))
        channel = bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)
        await channel.send(embed=build_embed(sha, messages, files))
        await bot.db.meta_set("last_announced_commit", sha)
        log.info("Announced update %s", sha[:7])
    except Exception:
        log.exception("Could not post the update announcement (does the bot have access to channel %s?)", CHANNEL_ID)
