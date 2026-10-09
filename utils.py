import random
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
_TOKEN = re.compile(r"(\d+)\s*([wdhms])")
_rng = random.SystemRandom()


def parse_duration(text: str):
    """'1d12h', '30 m', '2h, 15m' -> seconds. Returns None if it can't be read."""
    text = text.strip().lower()
    matches = _TOKEN.findall(text)
    if not matches:
        return None
    leftover = re.sub(r"[\s,]+", "", _TOKEN.sub("", text))
    if leftover:
        return None
    return sum(int(n) * _UNITS[u] for n, u in matches)


def weighted_order(entries):
    """Order (user_id, weight) pairs randomly, where a higher weight is
    proportionally more likely to come first (Efraimidis-Spirakis)."""
    keyed = [(_rng.random() ** (1.0 / max(weight, 1)), uid) for uid, weight in entries]
    keyed.sort(reverse=True)
    return [uid for _, uid in keyed]


def prepare_dsn(url: str):
    """Make a Render/Neon style URL safe for asyncpg. Returns (dsn, ssl_mode)."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    sslmode = query.pop("sslmode", None)
    query.pop("channel_binding", None)
    host = parts.hostname or ""
    if sslmode in ("require", "verify-ca", "verify-full"):
        ssl = "require"
    elif sslmode is None and host.endswith((".render.com", ".neon.tech")):
        ssl = "require"
    else:
        ssl = None
    return urlunsplit(parts._replace(query=urlencode(query))), ssl
