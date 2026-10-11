"""Public website pages: the commands reference and the live giveaways list."""
import asyncio
import time
from pathlib import Path

from flask import Response, render_template_string
from markupsafe import Markup

HERE = Path(__file__).parent

BASE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#7c3aed">
<title>{{ title }}: Giftly</title>
<link rel="icon" href="/logo.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fredoka:wght@500;600&family=Nunito:wght@400;600;800&display=swap">
<link rel="stylesheet" href="/style.css">
</head><body>
<header class="bar"><div class="in">
<a class="brand" href="/"><img src="/logo.png" alt="">Giftly</a>
<nav><a href="/commands">Commands</a><a href="/giveaways">Giveaways</a><a href="/terms">Terms</a><a class="btn small" href="/invite">Add to Discord</a></nav>
</div></header>
<main class="page">{{ body }}</main>
<footer>Made by Dormexed Productions. Giftly is not affiliated with Discord Inc.</footer>
</body></html>"""

COMMANDS = [
    ("Run giveaways", [
        ("/giveaway create", "Make one with a form. Supports several prizes and prize codes."),
        ("/giveaway start", "Make one with options. Roles, minimum account age, vote and message requirements."),
        ("/giveaway drop", "A quick drop where the first person to press the button wins."),
        ("/giveaway end", "Finish one early and draw the winners."),
        ("/giveaway reroll", "Draw again, or replace a specific winner."),
        ("/giveaway edit", "Change the prize, winner count or end time."),
        ("/giveaway cancel", "Stop one without drawing."),
    ]),
    ("Keep an eye on them", [
        ("/giveaway list", "See what's running right now."),
        ("/giveaway info", "Details of one giveaway."),
        ("/giveaway stats", "Entries over time, total entries and bonus entrants."),
        ("/giveaway entrants", "Download everyone who entered as a spreadsheet."),
        ("/giveaway awards", "See which prize codes went to whom."),
        ("/giveaway mystats", "Your own entries and wins in the server."),
    ]),
    ("Save time", [
        ("/giveaway template-save", "Save a giveaway's setup as a template."),
        ("/giveaway template-use", "Start a giveaway from a template."),
        ("/giveaway repeat-start", "Run a template again every day, week or month."),
        ("/giveaway repeat-list", "See what's repeating."),
        ("/giveaway repeat-stop", "Stop a repeating giveaway."),
    ]),
    ("Set it up", [
        ("/giveaway settings", "Roles, look, messages, defaults, claim window and the public listing."),
        ("/giveaway bonus-add", "Give a role bonus entries in every giveaway."),
        ("/giveaway bonus-remove", "Take a bonus role away."),
    ]),
    ("About Giftly", [
        ("/help", "A quick tour of the commands."),
        ("/invite", "Add Giftly to another server."),
        ("/support", "Join the support server."),
        ("/about", "Servers, ping and uptime."),
        ("/vote", "Vote for Giftly on top.gg."),
    ]),
]

COMMANDS_BODY = """<h1>Commands</h1>
<p class="lede">Everything Giftly can do. Most of these need the Manage Server permission, or the giveaway manager role you pick in settings.</p>
{% for title, rows in groups %}<h2>{{ title }}</h2>
<div class="cmd">{% for name, text in rows %}<code>{{ name }}</code><span>{{ text }}</span>{% endfor %}</div>{% endfor %}"""

GIVEAWAYS_BODY = """<h1>Live giveaways</h1>
<p class="lede">Giveaways from servers that chose to list them. Join the server to enter.</p>
{% if rows %}<div class="grid">{% for r in rows %}<article class="card">
<h3>{{ r.prize }}</h3>
<p class="meta">{{ r.guild_name or 'A Discord server' }}</p>
<p class="meta">{{ '{:,}'.format(r.entry_count) }} entries, {{ r.winners }} winner{{ '' if r.winners == 1 else 's' }}</p>
<p class="meta">Ends <time class="t" datetime="{{ r.ends_at.isoformat() }}">{{ r.ends_at.strftime('%b %d, %H:%M UTC') }}</time></p>
{% if r.public_invite and r.public_invite.startswith('https://discord') %}<a class="btn small" href="{{ r.public_invite }}" rel="noopener nofollow">Join server</a>{% endif %}
</article>{% endfor %}</div>
{% else %}<div class="empty"><b>Nothing listed right now.</b><br>Server admins can turn on the public listing in <code>/giveaway settings</code> under Look and feel, and add a server invite.</div>{% endif %}
<script>document.querySelectorAll('time.t').forEach(function(t){var s=(new Date(t.dateTime)-Date.now())/1000;if(s<=0){t.textContent='any moment now';return}var d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);t.textContent='in '+(d?d+'d ':'')+(h||d?h+'h ':'')+m+'m'});</script>"""

_cache = {"at": 0.0, "rows": []}


def page(title, body):
    return render_template_string(BASE, title=title, body=Markup(body))


def listings(bot):
    if time.time() - _cache["at"] < 30:
        return _cache["rows"]
    try:
        future = asyncio.run_coroutine_threadsafe(bot.db.public_giveaways(), bot.loop)
        _cache.update(at=time.time(), rows=[dict(r) for r in future.result(timeout=8)])
    except Exception:
        pass
    return _cache["rows"]


class Row(dict):
    __getattr__ = dict.get


def register(web, bot):
    @web.route("/style.css")
    def style():
        return Response((HERE / "style.css").read_text(encoding="utf-8"), mimetype="text/css")

    @web.route("/commands")
    def commands_page():
        return page("Commands", render_template_string(COMMANDS_BODY, groups=COMMANDS))

    @web.route("/giveaways")
    def giveaways_page():
        rows = [Row(r) for r in listings(bot)]
        return page("Live giveaways", render_template_string(GIVEAWAYS_BODY, rows=rows))
