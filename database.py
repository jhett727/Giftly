import asyncpg

from utils import prepare_dsn

SCHEMA = """
CREATE TABLE IF NOT EXISTS giveaways (
    id               SERIAL PRIMARY KEY,
    guild_id         BIGINT NOT NULL,
    channel_id       BIGINT NOT NULL,
    message_id       BIGINT UNIQUE,
    host_id          BIGINT NOT NULL,
    prize            TEXT NOT NULL,
    description      TEXT,
    winners          INT NOT NULL DEFAULT 1,
    ends_at          TIMESTAMPTZ NOT NULL,
    ended            BOOLEAN NOT NULL DEFAULT FALSE,
    cancelled        BOOLEAN NOT NULL DEFAULT FALSE,
    required_role_id BIGINT,
    bonus_role_id    BIGINT,
    bonus_entries    INT NOT NULL DEFAULT 0,
    winner_ids       BIGINT[] NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS giveaways_guild_idx ON giveaways (guild_id);

CREATE TABLE IF NOT EXISTS entries (
    giveaway_id INT NOT NULL REFERENCES giveaways(id) ON DELETE CASCADE,
    user_id     BIGINT NOT NULL,
    weight      INT NOT NULL DEFAULT 1,
    joined_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (giveaway_id, user_id)
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id     BIGINT PRIMARY KEY,
    color        INT NOT NULL DEFAULT 8141549,
    ping_role_id BIGINT
);

ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS image_url TEXT;
ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS min_account_days INT NOT NULL DEFAULT 0;
ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS min_server_days INT NOT NULL DEFAULT 0;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS manager_role_id BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS blacklist_role_id BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS button_label TEXT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS button_emoji TEXT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS footer_text TEXT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS win_message TEXT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS no_winner_message TEXT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS dm_winners BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS bypass_role_id BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS default_channel_id BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS default_duration TEXT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS default_winners INT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS show_entries BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS stack_bonuses BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS author_text TEXT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS thumbnail_url TEXT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS dm_message TEXT;

CREATE TABLE IF NOT EXISTS bot_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guild_bonus_roles (
    guild_id BIGINT NOT NULL,
    role_id  BIGINT NOT NULL,
    entries  INT NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);
"""

SETTING_TYPES = {
    "color": "int", "ping_role_id": "bigint", "manager_role_id": "bigint",
    "blacklist_role_id": "bigint", "button_label": "text", "button_emoji": "text",
    "footer_text": "text", "win_message": "text", "no_winner_message": "text",
    "dm_winners": "boolean", "bypass_role_id": "bigint", "default_channel_id": "bigint",
    "default_duration": "text", "default_winners": "int", "show_entries": "boolean",
    "stack_bonuses": "boolean", "author_text": "text", "thumbnail_url": "text", "dm_message": "text",
}


class Database:
    def __init__(self, url: str):
        self.url = url
        self.pool = None

    async def connect(self):
        dsn, ssl = prepare_dsn(self.url)
        options = dict(min_size=0, max_size=5, command_timeout=30,
                       max_inactive_connection_lifetime=60)
        if ssl:
            options["ssl"] = ssl
        self.pool = await asyncpg.create_pool(dsn, **options)
        await self.pool.execute(SCHEMA)

    async def close(self):
        if self.pool:
            await self.pool.close()

    # ---- giveaways ----
    async def create(self, **f):
        return await self.pool.fetchrow(
            """INSERT INTO giveaways (guild_id, channel_id, host_id, prize, description,
                                      winners, ends_at, required_role_id, bonus_role_id, bonus_entries,
                                      image_url, min_account_days, min_server_days)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING *""",
            f["guild_id"], f["channel_id"], f["host_id"], f["prize"], f["description"],
            f["winners"], f["ends_at"], f["required_role_id"], f["bonus_role_id"], f["bonus_entries"],
            f["image_url"], f["min_account_days"], f["min_server_days"])

    async def set_message(self, gid, message_id):
        await self.pool.execute("UPDATE giveaways SET message_id=$2 WHERE id=$1", gid, message_id)

    async def delete(self, gid):
        await self.pool.execute("DELETE FROM giveaways WHERE id=$1", gid)

    async def get(self, gid):
        return await self.pool.fetchrow("SELECT * FROM giveaways WHERE id=$1", gid)

    async def get_by_message(self, message_id):
        return await self.pool.fetchrow("SELECT * FROM giveaways WHERE message_id=$1", message_id)

    async def active_schedule(self):
        return await self.pool.fetch(
            "SELECT id, ends_at FROM giveaways WHERE NOT ended AND NOT cancelled")

    async def active(self, guild_id):
        return await self.pool.fetch(
            """SELECT g.*, (SELECT count(*) FROM entries e WHERE e.giveaway_id = g.id) AS entry_count
               FROM giveaways g
               WHERE g.guild_id=$1 AND NOT g.ended AND NOT g.cancelled
               ORDER BY g.ends_at LIMIT 15""", guild_id)

    async def search(self, guild_id, mode):
        where = {"active": "NOT ended AND NOT cancelled", "ended": "ended", "all": "TRUE"}[mode]
        return await self.pool.fetch(
            f"SELECT id, prize FROM giveaways WHERE guild_id=$1 AND {where} ORDER BY id DESC LIMIT 100",
            guild_id)

    async def finish(self, gid, winner_ids):
        return await self.pool.fetchrow(
            """UPDATE giveaways SET ended=TRUE, winner_ids=$2, ends_at=LEAST(ends_at, now())
               WHERE id=$1 RETURNING *""", gid, winner_ids)

    async def set_winners(self, gid, winner_ids):
        return await self.pool.fetchrow(
            "UPDATE giveaways SET winner_ids=$2 WHERE id=$1 RETURNING *", gid, winner_ids)

    async def cancel(self, gid):
        return await self.pool.fetchrow(
            "UPDATE giveaways SET cancelled=TRUE WHERE id=$1 RETURNING *", gid)

    async def update(self, gid, prize, winners, ends_at):
        return await self.pool.fetchrow(
            "UPDATE giveaways SET prize=$2, winners=$3, ends_at=$4 WHERE id=$1 RETURNING *",
            gid, prize, winners, ends_at)

    # ---- entries ----
    async def add_entry(self, gid, user_id, weight):
        result = await self.pool.execute(
            """INSERT INTO entries (giveaway_id, user_id, weight) VALUES ($1,$2,$3)
               ON CONFLICT DO NOTHING""", gid, user_id, weight)
        return result.endswith(" 1")

    async def remove_entry(self, gid, user_id):
        result = await self.pool.execute(
            "DELETE FROM entries WHERE giveaway_id=$1 AND user_id=$2", gid, user_id)
        return result.endswith(" 1")

    async def entry_count(self, gid):
        return await self.pool.fetchval("SELECT count(*) FROM entries WHERE giveaway_id=$1", gid)

    async def entries(self, gid):
        rows = await self.pool.fetch(
            "SELECT user_id, weight FROM entries WHERE giveaway_id=$1", gid)
        return [(r["user_id"], r["weight"]) for r in rows]

    # ---- settings ----
    async def get_settings(self, guild_id):
        return await self.pool.fetchrow("SELECT * FROM guild_settings WHERE guild_id=$1", guild_id)

    async def set_setting(self, guild_id, field, value):
        kind = SETTING_TYPES[field]  # only known columns are ever interpolated
        await self.pool.execute(
            f"""INSERT INTO guild_settings (guild_id, {field}) VALUES ($1, $2::{kind})
                ON CONFLICT (guild_id) DO UPDATE SET {field} = $2::{kind}""",
            guild_id, value)

    async def reset_settings(self, guild_id):
        await self.pool.execute("DELETE FROM guild_settings WHERE guild_id=$1", guild_id)
        await self.pool.execute("DELETE FROM guild_bonus_roles WHERE guild_id=$1", guild_id)

    async def bonus_list(self, guild_id):
        rows = await self.pool.fetch(
            "SELECT role_id, entries FROM guild_bonus_roles WHERE guild_id=$1", guild_id)
        return {r["role_id"]: r["entries"] for r in rows}

    async def bonus_set(self, guild_id, role_id, entries):
        await self.pool.execute(
            """INSERT INTO guild_bonus_roles (guild_id, role_id, entries) VALUES ($1,$2,$3)
               ON CONFLICT (guild_id, role_id) DO UPDATE SET entries=$3""", guild_id, role_id, entries)

    async def bonus_remove(self, guild_id, role_id):
        result = await self.pool.execute(
            "DELETE FROM guild_bonus_roles WHERE guild_id=$1 AND role_id=$2", guild_id, role_id)
        return result.endswith(" 1")

    # ---- bot metadata ----
    async def meta_get(self, key):
        return await self.pool.fetchval("SELECT value FROM bot_meta WHERE key=$1", key)

    async def meta_set(self, key, value):
        await self.pool.execute(
            """INSERT INTO bot_meta (key, value) VALUES ($1, $2)
               ON CONFLICT (key) DO UPDATE SET value=$2""", key, value)
