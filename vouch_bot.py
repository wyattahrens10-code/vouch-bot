import os
import time
import sqlite3
import logging
import random
import string
import re
from dataclasses import dataclass
from typing import Optional, List

import discord
from discord import app_commands
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional but recommended for fast slash commands

logging.basicConfig(level=logging.INFO)

DB_DIR = "/data"
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "vouchbot.db")

TRADE_EXPIRE_SECONDS = 3 * 60 * 60  # 3 hours

# Trade channel reminder (anti-spam)
TRADE_REMINDER_COOLDOWN = 30 * 60  # 30 minutes
TRADE_REMINDER_MIN_MESSAGES = 12   # only remind after activity
_last_trade_reminder_ts = 0
_trade_chat_counter = 0

# Report system (scam / sketchy behavior)
REPORT_CATEGORY_ID = 1460823073765720260  # Trading Hub category
MOD_ROLE_ID = 1460828375613706403
TRIAL_MOD_ROLE_ID = 1460828827206025329

# Auto-VC system (join-to-create)
CREATE_VC_TRIGGER_CHANNEL_ID = 1469166397492957234  # your ➕Create VC channel ID
TEMP_VC_BASE_NAME = "Squad VC"  # created channels will be: Squad VC 1, Squad VC 2, ...


intents = discord.Intents.default()
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logging.exception("App command error: %s", error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send("⚠️ Error running that command. Check bot logs.", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ Error running that command. Check bot logs.", ephemeral=True)
    except Exception:
        pass

# -------------------- DB --------------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        # Core config
        con.execute("""
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER PRIMARY KEY,
            vouch_channel_id INTEGER,
            trade_channel_id INTEGER,
            report_receipts_channel_id INTEGER,
            role_new_id INTEGER,
            role_verified_id INTEGER,
            role_trusted_id INTEGER,
            thresh_new INTEGER DEFAULT 1,
            thresh_verified INTEGER DEFAULT 5,
            thresh_trusted INTEGER DEFAULT 15
        )
        """)

        # Trades lifecycle
        con.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            trade_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            opener_id INTEGER NOT NULL,
            partner_id INTEGER NOT NULL,
            status TEXT NOT NULL, -- pending|active|completed|declined|expired|cancelled
            accepted INTEGER NOT NULL DEFAULT 0,
            opener_confirmed INTEGER NOT NULL DEFAULT 0,
            partner_confirmed INTEGER NOT NULL DEFAULT 0,
            channel_id INTEGER,
            message_id INTEGER,
            created_at INTEGER NOT NULL
        )
        """)

        # Vouches (trade_id required for real vouches, stars required)
        con.execute("""
        CREATE TABLE IF NOT EXISTS vouches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            trade_id TEXT,
            target_id INTEGER NOT NULL,
            voucher_id INTEGER NOT NULL,
            stars INTEGER NOT NULL DEFAULT 5,
            note TEXT,
            proof_url TEXT,
            created_at INTEGER NOT NULL
        )
        """)

        # Profiles (Embark ID)
        con.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            embark_id TEXT,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """)
        # Scam Reports
        con.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            trade_id TEXT NOT NULL,
            reporter_id INTEGER NOT NULL,
            opener_id INTEGER NOT NULL,
            partner_id INTEGER NOT NULL,
            description TEXT NOT NULL,
            trade_details TEXT,
            proof_url TEXT,
            channel_id INTEGER,
            message_id INTEGER,
            created_at INTEGER NOT NULL,
            resolved_at INTEGER,
            resolved_by INTEGER
        )
        """)


        
        # Temporary voice channels created by the bot
        con.execute("""
        CREATE TABLE IF NOT EXISTS temp_vcs (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (guild_id, channel_id)
        )
        """)
# --- Safe migrations for your existing DB ---
        try:
            con.execute("ALTER TABLE guild_config ADD COLUMN report_receipts_channel_id INTEGER")
        except sqlite3.OperationalError:
            pass

        try:
            con.execute("ALTER TABLE guild_config ADD COLUMN trade_channel_id INTEGER")
        except sqlite3.OperationalError:
            pass

        try:
            con.execute("ALTER TABLE vouches ADD COLUMN trade_id TEXT")
        except sqlite3.OperationalError:
            pass

        try:
            con.execute("ALTER TABLE vouches ADD COLUMN stars INTEGER NOT NULL DEFAULT 5")
        except sqlite3.OperationalError:
            pass

        # One vouch per trade per voucher (prevents spam for same trade)
        con.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_one_vouch_per_trade_per_voucher
        ON vouches (guild_id, trade_id, voucher_id)
        WHERE trade_id IS NOT NULL
        """)

        con.commit()

def get_config(guild_id: int):
    with db() as con:
        row = con.execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)).fetchone()
        if row is None:
            con.execute("INSERT INTO guild_config (guild_id) VALUES (?)", (guild_id,))
            con.commit()
            row = con.execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)).fetchone()
        return row

def set_config_value(guild_id: int, key: str, value: int):
    with db() as con:
        con.execute(f"UPDATE guild_config SET {key} = ? WHERE guild_id = ?", (value, guild_id))
        con.commit()

# -------------------- Profile helpers (Embark ID) --------------------
def set_embark_id(guild_id: int, user_id: int, embark_id: str):
    now = int(time.time())
    with db() as con:
        con.execute(
            "INSERT INTO profiles (guild_id, user_id, embark_id, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET embark_id=excluded.embark_id, updated_at=excluded.updated_at",
            (guild_id, user_id, embark_id, now)
        )
        con.commit()

def get_embark_id(guild_id: int, user_id: int) -> Optional[str]:
    with db() as con:
        row = con.execute(
            "SELECT embark_id FROM profiles WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        ).fetchone()
        return (row["embark_id"] if row and row["embark_id"] else None)
# -------------------- Report DB helpers --------------------
def create_report(
    guild_id: int,
    trade_id: str,
    reporter_id: int,
    opener_id: int,
    partner_id: int,
    description: str,
    trade_details: Optional[str],
    proof_url: Optional[str],
    channel_id: Optional[int] = None,
    message_id: Optional[int] = None,
) -> int:
    now = int(time.time())
    with db() as con:
        cur = con.execute(
            """
            INSERT INTO reports (
                guild_id, trade_id, reporter_id, opener_id, partner_id,
                description, trade_details, proof_url, channel_id, message_id, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (guild_id, trade_id, reporter_id, opener_id, partner_id, description, trade_details, proof_url, channel_id, message_id, now)
        )
        con.commit()
        return int(cur.lastrowid)

def attach_report_channel(report_id: int, channel_id: int, message_id: int):
    with db() as con:
        con.execute(
            "UPDATE reports SET channel_id=?, message_id=? WHERE id=?",
            (channel_id, message_id, report_id)
        )
        con.commit()

def get_report(report_id: int):
    with db() as con:
        return con.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()

def resolve_report(report_id: int, resolver_id: int):
    now = int(time.time())
    with db() as con:
        con.execute(
            "UPDATE reports SET resolved_at=?, resolved_by=? WHERE id=?",
            (now, resolver_id, report_id)
        )
        con.commit()


# -------------------- Temp VC helpers --------------------
def add_temp_vc(guild_id: int, channel_id: int):
    now = int(time.time())
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO temp_vcs (guild_id, channel_id, created_at) VALUES (?,?,?)",
            (guild_id, channel_id, now)
        )
        con.commit()

def remove_temp_vc(guild_id: int, channel_id: int):
    with db() as con:
        con.execute("DELETE FROM temp_vcs WHERE guild_id=? AND channel_id=?", (guild_id, channel_id))
        con.commit()

def is_temp_vc(guild_id: int, channel_id: int) -> bool:
    with db() as con:
        row = con.execute(
            "SELECT 1 FROM temp_vcs WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id)
        ).fetchone()
        return row is not None


# -------------------- Vouch DB helpers --------------------
def add_vouch(guild_id: int, trade_id: str, target_id: int, voucher_id: int, stars: int,
              note: Optional[str], proof_url: Optional[str]) -> None:
    now = int(time.time())
    with db() as con:
        con.execute(
            "INSERT INTO vouches (guild_id, trade_id, target_id, voucher_id, stars, note, proof_url, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (guild_id, trade_id, target_id, voucher_id, stars, note, proof_url, now)
        )
        con.commit()

def vouch_count(guild_id: int, target_id: int) -> int:
    with db() as con:
        row = con.execute(
            "SELECT COUNT(*) AS c FROM vouches WHERE guild_id = ? AND target_id = ? AND trade_id IS NOT NULL",
            (guild_id, target_id)
        ).fetchone()
        return int(row["c"])

def avg_stars(guild_id: int, target_id: int) -> float:
    with db() as con:
        row = con.execute(
            "SELECT AVG(stars) AS a FROM vouches WHERE guild_id = ? AND target_id = ? AND trade_id IS NOT NULL",
            (guild_id, target_id)
        ).fetchone()
        return float(row["a"] or 0.0)

def top_traders(guild_id: int, limit: int = 10):
    with db() as con:
        rows = con.execute(
            """
            SELECT target_id, COUNT(*) AS vouches, AVG(stars) AS avg_stars
            FROM vouches
            WHERE guild_id = ? AND trade_id IS NOT NULL
            GROUP BY target_id
            ORDER BY vouches DESC, avg_stars DESC
            LIMIT ?
            """,
            (guild_id, limit)
        ).fetchall()
        return [(int(r["target_id"]), int(r["vouches"]), float(r["avg_stars"] or 0.0)) for r in rows]

# -------------------- Trade DB helpers --------------------
def make_trade_id() -> str:
    return "T-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))

def create_trade(guild_id: int, opener_id: int, partner_id: int) -> str:
    trade_id = make_trade_id()
    now = int(time.time())
    with db() as con:
        con.execute(
            "INSERT INTO trades (trade_id, guild_id, opener_id, partner_id, status, created_at) VALUES (?,?,?,?,?,?)",
            (trade_id, guild_id, opener_id, partner_id, "pending", now)
        )
        con.commit()
    return trade_id

def set_trade_message(trade_id: str, channel_id: int, message_id: int):
    with db() as con:
        con.execute(
            "UPDATE trades SET channel_id=?, message_id=? WHERE trade_id=?",
            (channel_id, message_id, trade_id)
        )
        con.commit()

def get_trade(trade_id: str):
    with db() as con:
        return con.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,)).fetchone()

def update_trade(trade_id: str, **fields):
    if not fields:
        return
    keys = ", ".join([f"{k}=?" for k in fields.keys()])
    vals = list(fields.values())
    with db() as con:
        con.execute(f"UPDATE trades SET {keys} WHERE trade_id=?", (*vals, trade_id))
        con.commit()

def find_expirable_trades(now_ts: int) -> List[sqlite3.Row]:
    cutoff = now_ts - TRADE_EXPIRE_SECONDS
    with db() as con:
        rows = con.execute(
            """
            SELECT * FROM trades
            WHERE status IN ('pending','active')
              AND created_at <= ?
              AND channel_id IS NOT NULL
              AND message_id IS NOT NULL
            """,
            (cutoff,)
        ).fetchall()
        return rows

def last_trades_for_user(guild_id: int, user_id: int, limit: int = 5) -> List[sqlite3.Row]:
    with db() as con:
        rows = con.execute(
            """
            SELECT trade_id, status, created_at, opener_id, partner_id
            FROM trades
            WHERE guild_id = ?
              AND (opener_id = ? OR partner_id = ?)
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (guild_id, user_id, user_id, limit)
        ).fetchall()
        return rows
def trade_stats_for_user(guild_id: int, user_id: int):
    with db() as con:
        rows = con.execute(
            """
            SELECT status, COUNT(*) AS c
            FROM trades
            WHERE guild_id = ?
              AND (opener_id = ? OR partner_id = ?)
            GROUP BY status
            """,
            (guild_id, user_id, user_id)
        ).fetchall()

    stats = {
        "total": 0,
        "completed": 0,
        "failed": 0
    }

    for r in rows:
        count = int(r["c"])
        stats["total"] += count
        status = str(r["status"])
        if status == "completed":
            stats["completed"] += count
        elif status in ("cancelled", "expired", "declined"):
            stats["failed"] += count

    return stats

# -------------------- Role logic --------------------
@dataclass
class Tier:
    name: str
    threshold: int
    role_id: Optional[int]

def get_tiers(cfg) -> list[Tier]:
    return [
        Tier("Trusted Trader", int(cfg["thresh_trusted"] or 15), int(cfg["role_trusted_id"] or 0) or None),
        Tier("Verified Trader", int(cfg["thresh_verified"] or 5), int(cfg["role_verified_id"] or 0) or None),
        Tier("New Trader", int(cfg["thresh_new"] or 1), int(cfg["role_new_id"] or 0) or None),
    ]

async def apply_roles(member: discord.Member, total_vouches: int) -> Optional[str]:
    cfg = get_config(member.guild.id)
    tiers = get_tiers(cfg)

    chosen: Optional[Tier] = None
    for t in tiers:
        if total_vouches >= t.threshold and t.role_id:
            chosen = t
            break

    role_new = member.guild.get_role(int(cfg["role_new_id"] or 0)) if cfg["role_new_id"] else None
    role_ver = member.guild.get_role(int(cfg["role_verified_id"] or 0)) if cfg["role_verified_id"] else None
    role_tru = member.guild.get_role(int(cfg["role_trusted_id"] or 0)) if cfg["role_trusted_id"] else None
    roles_all = [r for r in [role_new, role_ver, role_tru] if r]

    to_remove = [r for r in roles_all if r in member.roles]
    if to_remove:
        await member.remove_roles(*to_remove, reason="Vouch tier update")

    if chosen and chosen.role_id:
        role = member.guild.get_role(chosen.role_id)
        if role:
            await member.add_roles(role, reason="Vouch tier update")
            return chosen.name

    return None

# -------------------- Role display helpers --------------------
REGION_ROLE_NAMES = ["🌎NA", "🌎EU", "🌎OCE", "🌎Asia", "🌎SA"]
PLATFORM_ROLE_NAMES = ["🎮Console", "🖥️PC"]
PLAYSTYLE_ROLE_NAMES = ["🟢 Casual", "🔴 Sweaty", "💰Traders", "🧠 Helper"]
STAFF_ROLE_NAMES = ["🔨 Mods", "🧪 Trial Mods"]

def _has_role_name(member: discord.Member, role_name: str) -> bool:
    return any(r.name == role_name for r in member.roles)

def pick_single_role_name(member: discord.Member, names: list[str]) -> Optional[str]:
    for n in names:
        if _has_role_name(member, n):
            return n
    return None

def pick_multi_role_names(member: discord.Member, names: list[str]) -> list[str]:
    return [n for n in names if _has_role_name(member, n)]

def trader_tier_label(member: Optional[discord.Member], total_vouches: int, cfg) -> str:
    # Prefer the actual tier roles on the member (most accurate)
    if member:
        for cfg_key in ("role_trusted_id", "role_verified_id", "role_new_id"):
            rid = int(cfg[cfg_key] or 0)
            if rid and any(r.id == rid for r in member.roles):
                role = member.guild.get_role(rid)
                return role.name if role else "Trader Tier"

    # Fallback: compute from thresholds (in case roles weren't applied yet)
    thresh_new = int(cfg["thresh_new"] or 1)
    thresh_ver = int(cfg["thresh_verified"] or 5)
    thresh_tru = int(cfg["thresh_trusted"] or 15)

    if total_vouches >= thresh_tru:
        return "🛡️ Trusted Trader"
    if total_vouches >= thresh_ver:
        return "🪙 Verified Trader"
    if total_vouches >= thresh_new:
        return "🆕 New Trader"
    return "Unranked"

def user_badges(member: Optional[discord.Member]) -> dict:
    if not member:
        return {"region": None, "platform": None, "playstyle": [], "staff": None}

    region = pick_single_role_name(member, REGION_ROLE_NAMES)
    platform = pick_single_role_name(member, PLATFORM_ROLE_NAMES)
    playstyle = pick_multi_role_names(member, PLAYSTYLE_ROLE_NAMES)
    staff = pick_single_role_name(member, STAFF_ROLE_NAMES)

    return {"region": region, "platform": platform, "playstyle": playstyle, "staff": staff}


# -------------------- Auto-VC helpers --------------------
def next_temp_vc_name(guild: discord.Guild) -> str:
    """Find next available 'Squad VC N' name by scanning existing voice channels."""
    pattern = re.compile(rf"^{re.escape(TEMP_VC_BASE_NAME)}\s+(\d+)$", re.IGNORECASE)
    max_n = 0
    for ch in guild.voice_channels:
        m = pattern.match(ch.name or "")
        if m:
            try:
                max_n = max(max_n, int(m.group(1)))
            except Exception:
                pass
    return f"{TEMP_VC_BASE_NAME} {max_n + 1}"

# -------------------- Embeds --------------------
def build_trade_embed(guild: discord.Guild, trade_id: str) -> discord.Embed:
    trade = get_trade(trade_id)
    opener = guild.get_member(int(trade["opener_id"])) if trade else None
    partner = guild.get_member(int(trade["partner_id"])) if trade else None

    status = trade["status"]
    oc = bool(trade["opener_confirmed"])
    pc = bool(trade["partner_confirmed"])

    title_map = {
        "pending": "🧾 Trade Request",
        "active": "🤝 Trade Active",
        "completed": "✅ Trade Completed",
        "declined": "❌ Trade Declined",
        "expired": "⏳ Trade Expired",
        "cancelled": "🚫 Trade Cancelled",
    }

    embed = discord.Embed(title=title_map.get(status, "Trade"), color=discord.Color.blurple())
    embed.add_field(name="Trade ID", value=f"`{trade_id}`", inline=False)
    embed.add_field(name="Opener", value=opener.mention if opener else f"<@{trade['opener_id']}>", inline=True)
    embed.add_field(name="Partner", value=partner.mention if partner else f"<@{trade['partner_id']}>", inline=True)

    opener_eid = get_embark_id(guild.id, int(trade["opener_id"])) if trade else None
    partner_eid = get_embark_id(guild.id, int(trade["partner_id"])) if trade else None
    embed.add_field(name="Opener Embark ID", value=f"`{opener_eid}`" if opener_eid else "*Not set*", inline=True)
    embed.add_field(name="Partner Embark ID", value=f"`{partner_eid}`" if partner_eid else "*Not set*", inline=True)

    if status == "pending":
        embed.add_field(name="Status", value="Waiting for partner to accept or decline.", inline=False)
        embed.set_footer(text="Partner: click Accept/Decline • Auto-expires in 3 hours")
    elif status == "active":
        embed.add_field(name="Status", value="Active — complete the trade then both confirm.", inline=False)
        embed.add_field(
            name="Confirmations",
            value=f"Opener: {'✅' if oc else '⏳'} • Partner: {'✅' if pc else '⏳'}",
            inline=False
        )
        embed.set_footer(text="Only opener/partner can confirm • Staff can force close • Auto-expires in 3 hours")
    elif status == "completed":
        embed.add_field(name="Status", value="Completed ✅ — you may now vouch using this Trade ID.", inline=False)
        embed.set_footer(text="Use /vouch with the Trade ID (required)")
    elif status == "declined":
        embed.add_field(name="Status", value="Declined ❌", inline=False)
    elif status == "expired":
        embed.add_field(name="Status", value="Expired ⏳ — not completed within 3 hours.", inline=False)
    elif status == "cancelled":
        embed.add_field(name="Status", value="Cancelled 🚫", inline=False)

    if partner:
        embed.set_thumbnail(url=partner.display_avatar.url)

    return embed

def build_vouch_embed(
    guild: discord.Guild,
    trade_id: str,
    trader: discord.Member,
    voucher: discord.Member,
    stars: int,
    total: int,
    avg: float,
    tier_update: Optional[str],
    note: Optional[str],
    proof_url: Optional[str]
) -> discord.Embed:
    star_line = "⭐" * stars + "☆" * (5 - stars)

    embed = discord.Embed(
        title="✅ Vouch Logged",
        description=f"{star_line}  **({stars}/5)**",
        color=discord.Color.green()
    )
    embed.add_field(name="Trade ID", value=f"`{trade_id}`", inline=False)

    trader_eid = get_embark_id(guild.id, trader.id)
    embed.add_field(name="Trader", value=trader.mention, inline=True)
    embed.add_field(name="Embark ID", value=f"`{trader_eid}`" if trader_eid else "*Not set*", inline=True)

    embed.add_field(name="Vouched By", value=voucher.mention, inline=True)
    embed.add_field(name="Total Vouches", value=str(total), inline=True)
    embed.add_field(name="Avg Rating", value=f"**{avg:.2f}/5**", inline=True)

    if tier_update:
        embed.add_field(name="Tier Update", value=f"Now: **{tier_update}**", inline=False)

    if note:
        embed.add_field(name="Note", value=note[:1024], inline=False)
    if proof_url:
        embed.add_field(name="Proof", value=proof_url[:1024], inline=False)

    embed.set_thumbnail(url=trader.display_avatar.url)
    embed.set_footer(text="Use /rep privately • Trade at your own risk")
    return embed

# -------------------- Trade ID extraction (fixes Railway restart issues) --------------------
def trade_id_from_message(interaction: discord.Interaction) -> Optional[str]:
    if not interaction.message or not interaction.message.embeds:
        return None

    emb = interaction.message.embeds[0]
    if not emb.fields:
        return None

    for f in emb.fields:
        if (f.name or "").lower().strip() == "trade id":
            raw = (f.value or "").strip()
            return raw.strip("`").strip().upper()

    return None


# -------------------- Reports UI --------------------
def _parse_report_id_from_channel(channel: discord.abc.GuildChannel) -> Optional[int]:
    # topic format: "report_id=123 trade_id=T-ABC123"
    try:
        topic = getattr(channel, "topic", None) or ""
        m = re.search(r"report_id=(\d+)", topic)
        return int(m.group(1)) if m else None
    except Exception:
        return None

class ScamReportModal(discord.ui.Modal, title="Report Trade Issue"):
    def __init__(self, trade_id: str):
        super().__init__(timeout=None)
        self.trade_id = trade_id

        self.what_happened = discord.ui.TextInput(
            label="What happened?",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=900,
            placeholder="Explain what happened (keep it clear + short)."
        )
        self.trade_details = discord.ui.TextInput(
            label="Trade details (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=400,
            placeholder="What items/GOOP were supposed to be traded?"
        )
        self.proof_url = discord.ui.TextInput(
            label="Proof link (clip recommended)",
            style=discord.TextStyle.short,
            required=False,
            max_length=200,
            placeholder="Paste a clip/link if you have it (strongly recommended)."
        )

        self.add_item(self.what_happened)
        self.add_item(self.trade_details)
        self.add_item(self.proof_url)

    async def on_submit(self, interaction: discord.Interaction):
        trade_id = self.trade_id.strip().upper()
        trade = get_trade(trade_id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)

        # Allow reporting on active trades only
        if trade["status"] != "active":
            return await interaction.response.send_message("Reports can only be filed while a trade is **Active**.", ephemeral=True)

        opener_id = int(trade["opener_id"])
        partner_id = int(trade["partner_id"])
        reporter_id = interaction.user.id

        if reporter_id not in (opener_id, partner_id):
            return await interaction.response.send_message("Only trade participants can file a report.", ephemeral=True)

        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message("Server not found.", ephemeral=True)

        # Create DB record first
        desc = str(self.what_happened.value).strip()
        details = str(self.trade_details.value).strip() if self.trade_details.value else None
        proof = str(self.proof_url.value).strip() if self.proof_url.value else None

        report_id = create_report(
            guild_id=guild.id,
            trade_id=trade_id,
            reporter_id=reporter_id,
            opener_id=opener_id,
            partner_id=partner_id,
            description=desc,
            trade_details=details,
            proof_url=proof,
        )

        # Find category
        category = guild.get_channel(REPORT_CATEGORY_ID)
        if category and not isinstance(category, discord.CategoryChannel):
            category = None

        # Create a unique channel name
        base_name = f"report-{trade_id.lower()}"
        name = base_name
        existing_names = {c.name for c in guild.text_channels}
        n = 2
        while name in existing_names:
            name = f"{base_name}-{n}"
            n += 1

        # Overwrites
        async def _get_member(uid: int) -> Optional[discord.Member]:
            m = guild.get_member(uid)
            if m:
                return m
            try:
                return await guild.fetch_member(uid)
            except Exception:
                return None

        me = guild.me or (guild.get_member(bot.user.id) if bot.user else None)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        if me:
            overwrites[me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
            )

        # IMPORTANT: use fetch_member so the reporter/opener/partner always get included,
        # even if they're not cached (no Members intent / cold start).
        reporter = await _get_member(reporter_id) or interaction.user
        opener = await _get_member(opener_id)
        partner = await _get_member(partner_id)

        for m in [reporter, opener, partner]:
            if m:
                overwrites[m] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                )

        mod_role = guild.get_role(MOD_ROLE_ID)
        trial_role = guild.get_role(TRIAL_MOD_ROLE_ID)
        for r in [mod_role, trial_role]:
            if r:
                overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

        topic = f"report_id={report_id} trade_id={trade_id}"
        report_channel = await guild.create_text_channel(
            name=name,
            category=category,
            topic=topic,
            overwrites=overwrites,
            reason=f"Scam report for {trade_id}"
        )

        # Build report embed
        accused_id = partner_id if reporter_id == opener_id else opener_id
        accused = guild.get_member(accused_id)
        accused_txt = accused.mention if accused else f"<@{accused_id}>"

        embed = discord.Embed(title="🚨 Trade Report Opened", color=discord.Color.red())
        embed.add_field(name="Report ID", value=f"`{report_id}`", inline=True)
        embed.add_field(name="Trade ID", value=f"`{trade_id}`", inline=True)
        embed.add_field(name="Reporter", value=interaction.user.mention, inline=True)
        embed.add_field(name="Other Trader", value=accused_txt, inline=True)
        embed.add_field(name="What happened", value=desc[:1024], inline=False)

        if details:
            embed.add_field(name="Trade details", value=details[:1024], inline=False)
        if proof:
            embed.add_field(name="Proof", value=proof[:1024], inline=False)
        else:
            embed.add_field(name="Proof", value="*Not provided yet — clip is strongly recommended.*", inline=False)

        embed.set_footer(text="Mods: use Mark Resolved when handled • You can add another trader if needed")

        view = ReportChannelView()

        ping = ""
        if mod_role:
            ping += mod_role.mention + " "
        if trial_role:
            ping += trial_role.mention + " "

        msg = await report_channel.send(
            content=(ping.strip() if ping else None),
            embed=embed,
            view=view
        )

        attach_report_channel(report_id, report_channel.id, msg.id)

        await interaction.response.send_message(
            f"✅ Report opened: {report_channel.mention}\n"
            f"**Tip:** A clip/link is strongly recommended. You can post it in that channel any time.",
            ephemeral=True
        )

class ReportChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Mark Resolved (Staff)", style=discord.ButtonStyle.success, custom_id="report_mark_resolved")
    async def mark_resolved(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not is_staff_member(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("This must be used in a report channel.", ephemeral=True)

        report_id = _parse_report_id_from_channel(channel)
        if not report_id:
            return await interaction.response.send_message("Couldn't find Report ID for this channel.", ephemeral=True)

        report = get_report(report_id)
        if not report:
            return await interaction.response.send_message("Report record not found.", ephemeral=True)

        # Update DB
        resolve_report(report_id, interaction.user.id)

        guild = interaction.guild
        cfg = get_config(guild.id)
        receipts_id = int(cfg["report_receipts_channel_id"] or 0)

        receipt_embed = discord.Embed(title="🧾 Report Receipt (Resolved)", color=discord.Color.green())
        receipt_embed.add_field(name="Report ID", value=f"`{report_id}`", inline=True)
        receipt_embed.add_field(name="Trade ID", value=f"`{report['trade_id']}`", inline=True)
        receipt_embed.add_field(name="Reporter", value=f"<@{int(report['reporter_id'])}>", inline=True)
        # show both traders for context
        receipt_embed.add_field(name="Opener", value=f"<@{int(report['opener_id'])}>", inline=True)
        receipt_embed.add_field(name="Partner", value=f"<@{int(report['partner_id'])}>", inline=True)
        receipt_embed.add_field(name="What happened", value=str(report["description"])[:1024], inline=False)

        if report["trade_details"]:
            receipt_embed.add_field(name="Trade details", value=str(report["trade_details"])[:1024], inline=False)
        if report["proof_url"]:
            receipt_embed.add_field(name="Proof", value=str(report["proof_url"])[:1024], inline=False)

        receipt_embed.add_field(name="Resolved By", value=interaction.user.mention, inline=True)

        sent_to = None
        if receipts_id:
            receipts_ch = guild.get_channel(receipts_id)
            if isinstance(receipts_ch, discord.TextChannel):
                await receipts_ch.send(embed=receipt_embed)
                sent_to = receipts_ch

        # confirm in-channel
        note = "✅ Marked as resolved."
        if sent_to:
            note += f" Receipt posted in {sent_to.mention}."
        else:
            note += " (No receipts channel set — ask an admin to run `/set_report_receipts_channel`.)"

        await interaction.response.send_message(note, ephemeral=True)

    @discord.ui.button(label="Add Other Trader (Staff)", style=discord.ButtonStyle.secondary, custom_id="report_add_other_trader")
    async def add_other_trader(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not is_staff_member(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        await interaction.response.send_message(
            "Use `/report_add_user @user` in this report channel to add someone else to the channel.",
            ephemeral=True
        )

# -------------------- Trade Views (Buttons) --------------------
class PendingTradeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Accept Trade", style=discord.ButtonStyle.success, custom_id="trade_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        trade_id = trade_id_from_message(interaction)
        if not trade_id:
            return await interaction.response.send_message("Couldn't read Trade ID from message.", ephemeral=True)

        trade = get_trade(trade_id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)
        if trade["status"] != "pending":
            return await interaction.response.send_message("This trade is no longer pending.", ephemeral=True)

        if interaction.user.id != int(trade["partner_id"]):
            return await interaction.response.send_message("Only the tagged partner can accept.", ephemeral=True)

        update_trade(trade_id, status="active", accepted=1)
        embed = build_trade_embed(interaction.guild, trade_id)
        await interaction.response.edit_message(embed=embed, view=ActiveTradeView())

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, custom_id="trade_decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        trade_id = trade_id_from_message(interaction)
        if not trade_id:
            return await interaction.response.send_message("Couldn't read Trade ID from message.", ephemeral=True)

        trade = get_trade(trade_id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)
        if trade["status"] != "pending":
            return await interaction.response.send_message("This trade is no longer pending.", ephemeral=True)

        if interaction.user.id != int(trade["partner_id"]):
            return await interaction.response.send_message("Only the tagged partner can decline.", ephemeral=True)

        update_trade(trade_id, status="declined")
        embed = build_trade_embed(interaction.guild, trade_id)
        await interaction.response.edit_message(embed=embed, view=None)

class ActiveTradeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Confirm Complete (Opener)", style=discord.ButtonStyle.primary, custom_id="trade_confirm_opener")
    async def confirm_opener(self, interaction: discord.Interaction, button: discord.ui.Button):
        trade_id = trade_id_from_message(interaction)
        if not trade_id:
            return await interaction.response.send_message("Couldn't read Trade ID from message.", ephemeral=True)

        trade = get_trade(trade_id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)
        if trade["status"] != "active":
            return await interaction.response.send_message("Trade is not active.", ephemeral=True)
        if interaction.user.id != int(trade["opener_id"]):
            return await interaction.response.send_message("Only the opener can press this.", ephemeral=True)

        update_trade(trade_id, opener_confirmed=1)
        await self._refresh_or_finalize(interaction, trade_id)

    @discord.ui.button(label="Confirm Complete (Partner)", style=discord.ButtonStyle.primary, custom_id="trade_confirm_partner")
    async def confirm_partner(self, interaction: discord.Interaction, button: discord.ui.Button):
        trade_id = trade_id_from_message(interaction)
        if not trade_id:
            return await interaction.response.send_message("Couldn't read Trade ID from message.", ephemeral=True)

        trade = get_trade(trade_id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)
        if trade["status"] != "active":
            return await interaction.response.send_message("Trade is not active.", ephemeral=True)
        if interaction.user.id != int(trade["partner_id"]):
            return await interaction.response.send_message("Only the partner can press this.", ephemeral=True)

        update_trade(trade_id, partner_confirmed=1)
        await self._refresh_or_finalize(interaction, trade_id)

    
    @discord.ui.button(label="Report Scam / Sketchy", style=discord.ButtonStyle.danger, custom_id="trade_report_scam")
    async def report_scam(self, interaction: discord.Interaction, button: discord.ui.Button):
        trade_id = trade_id_from_message(interaction)
        if not trade_id:
            return await interaction.response.send_message("Couldn't read Trade ID from message.", ephemeral=True)

        trade = get_trade(trade_id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)
        if trade["status"] != "active":
            return await interaction.response.send_message("Reports can only be filed while the trade is **Active**.", ephemeral=True)

        if interaction.user.id not in (int(trade["opener_id"]), int(trade["partner_id"])):
            return await interaction.response.send_message("Only trade participants can file a report.", ephemeral=True)

        await interaction.response.send_modal(ScamReportModal(trade_id))

    @discord.ui.button(label="Force Close (Staff)", style=discord.ButtonStyle.danger, custom_id="trade_force_close")
    async def force_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        trade_id = trade_id_from_message(interaction)
        if not trade_id:
            return await interaction.response.send_message("Couldn't read Trade ID from message.", ephemeral=True)

        trade = get_trade(trade_id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)

        update_trade(trade_id, status="cancelled")
        embed = build_trade_embed(interaction.guild, trade_id)
        embed.add_field(name="Staff Action", value=f"Force closed by {interaction.user.mention}", inline=False)
        await interaction.response.edit_message(embed=embed, view=None)

    async def _refresh_or_finalize(self, interaction: discord.Interaction, trade_id: str):
        trade = get_trade(trade_id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)

        if int(trade["opener_confirmed"]) == 1 and int(trade["partner_confirmed"]) == 1:
            update_trade(trade_id, status="completed")
            embed = build_trade_embed(interaction.guild, trade_id)
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            embed = build_trade_embed(interaction.guild, trade_id)
            await interaction.response.edit_message(embed=embed, view=self)

# -------------------- Auto-expire task --------------------
@tasks.loop(minutes=1)
async def expire_trades_loop():
    now_ts = int(time.time())
    rows = find_expirable_trades(now_ts)

    for trade in rows:
        trade_id = trade["trade_id"]
        latest = get_trade(trade_id)
        if not latest or latest["status"] not in ("pending", "active"):
            continue

        update_trade(trade_id, status="expired")

        try:
            guild = bot.get_guild(int(latest["guild_id"]))
            if not guild:
                continue
            channel = guild.get_channel(int(latest["channel_id"]))
            if not isinstance(channel, discord.TextChannel):
                continue
            msg = await channel.fetch_message(int(latest["message_id"]))
            await msg.edit(embed=build_trade_embed(guild, trade_id), view=None)
        except Exception as e:
            logging.warning(f"Failed to expire/edit trade {trade_id}: {e}")

# -------------------- Trade channel reminder (anti-spam) --------------------
@bot.event
async def on_message(message: discord.Message):
    global _last_trade_reminder_ts, _trade_chat_counter

    if message.author.bot or not message.guild:
        return

    cfg = get_config(message.guild.id)
    trade_channel_id = int(cfg["trade_channel_id"] or 0)

    if trade_channel_id and message.channel.id == trade_channel_id:
        _trade_chat_counter += 1
        now = int(time.time())

        if _trade_chat_counter >= TRADE_REMINDER_MIN_MESSAGES and (now - _last_trade_reminder_ts) >= TRADE_REMINDER_COOLDOWN:
            _last_trade_reminder_ts = now
            _trade_chat_counter = 0
            await message.channel.send(
                "🧾 **Found a Raider to trade with?** Use **`/trade @user`** to open a Trade Ticket.\n"
                "It keeps trades organized and unlocks vouches with a Trade ID ✅"
            )

    await bot.process_commands(message)


# -------------------- Auto-VC (join-to-create) --------------------
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # User joined the trigger channel -> create a temp VC and move them
    try:
        if after and after.channel and after.channel.id == CREATE_VC_TRIGGER_CHANNEL_ID:
            guild = member.guild
            trigger_ch = after.channel
            category = trigger_ch.category  # keep everything under the same category as the trigger channel

            # Create the new VC
            new_name = next_temp_vc_name(guild)
            new_vc = await guild.create_voice_channel(
                name=new_name,
                category=category,
                reason=f"Auto-VC created for {member} ({member.id})"
            )

            # Track in DB so we know which ones are safe to auto-delete
            add_temp_vc(guild.id, new_vc.id)

            # Move creator into the new VC
            await member.move_to(new_vc, reason="Moved to auto-created VC")

        # User left a channel -> delete it if it's an empty temp VC
        if before and before.channel and (after is None or after.channel != before.channel):
            ch = before.channel
            if ch and isinstance(ch, discord.VoiceChannel):
                if is_temp_vc(member.guild.id, ch.id) and len(ch.members) == 0:
                    try:
                        await ch.delete(reason="Auto-VC cleanup (empty)")
                    finally:
                        remove_temp_vc(member.guild.id, ch.id)
    except Exception as e:
        logging.warning(f"Auto-VC error: {e}")

# -------------------- Commands --------------------
def admin_only(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.administrator
def is_staff_member(member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    role_ids = {r.id for r in getattr(member, "roles", [])}
    return (MOD_ROLE_ID in role_ids) or (TRIAL_MOD_ROLE_ID in role_ids)



@bot.event
async def on_ready():
    init_db()
    logging.info(f"Logged in as {bot.user} (id: {bot.user.id})")

    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        logging.info(f"Synced commands to guild {GUILD_ID}")
    else:
        await bot.tree.sync()
        logging.info("Synced global commands (can take time to appear)")

    bot.add_view(PendingTradeView())
    bot.add_view(ActiveTradeView())
    bot.add_view(ReportChannelView())

    if not expire_trades_loop.is_running():
        expire_trades_loop.start()

# ---- Admin setup ----
@bot.tree.command(name="set_trade_channel", description="Admin: set the channel where trade tickets are posted")
async def set_trade_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not admin_only(interaction):
        return await interaction.response.send_message("Admin only.", ephemeral=True)

    set_config_value(interaction.guild.id, "trade_channel_id", channel.id)
    await interaction.response.send_message(f"Trade channel set to {channel.mention}.", ephemeral=True)

@bot.tree.command(name="set_vouch_channel", description="Admin: set the channel where vouches are posted")
async def set_vouch_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not admin_only(interaction):
        return await interaction.response.send_message("Admin only.", ephemeral=True)

    set_config_value(interaction.guild.id, "vouch_channel_id", channel.id)
    await interaction.response.send_message(f"Vouch channel set to {channel.mention}.", ephemeral=True)
@bot.tree.command(name="set_report_receipts_channel", description="Admin: set the channel where report receipts are posted")
async def set_report_receipts_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not admin_only(interaction):
        return await interaction.response.send_message("Admin only.", ephemeral=True)

    set_config_value(interaction.guild.id, "report_receipts_channel_id", channel.id)
    await interaction.response.send_message(f"Report receipts channel set to {channel.mention}.", ephemeral=True)



@bot.tree.command(name="setup_roles", description="Admin: set the role IDs for New/Verified/Trusted tiers")
async def setup_roles(interaction: discord.Interaction, new_role: discord.Role, verified_role: discord.Role, trusted_role: discord.Role):
    if not admin_only(interaction):
        return await interaction.response.send_message("Admin only.", ephemeral=True)

    gid = interaction.guild.id
    set_config_value(gid, "role_new_id", new_role.id)
    set_config_value(gid, "role_verified_id", verified_role.id)
    set_config_value(gid, "role_trusted_id", trusted_role.id)

    await interaction.response.send_message(
        f"Roles set:\n- New Trader: {new_role.mention}\n- Verified Trader: {verified_role.mention}\n- Trusted Trader: {trusted_role.mention}",
        ephemeral=True
    )

@bot.tree.command(name="set_thresholds", description="Admin: set vouch thresholds for each tier")
async def set_thresholds(interaction: discord.Interaction, new: int = 1, verified: int = 5, trusted: int = 15):
    if not admin_only(interaction):
        return await interaction.response.send_message("Admin only.", ephemeral=True)
    if not (0 <= new <= verified <= trusted):
        return await interaction.response.send_message("Use numbers like: new <= verified <= trusted.", ephemeral=True)

    gid = interaction.guild.id
    set_config_value(gid, "thresh_new", int(new))
    set_config_value(gid, "thresh_verified", int(verified))
    set_config_value(gid, "thresh_trusted", int(trusted))

    await interaction.response.send_message(
        f"Thresholds set:\n- New Trader: {new}+\n- Verified Trader: {verified}+\n- Trusted Trader: {trusted}+",
        ephemeral=True
    )

# ---- Help ----
@bot.tree.command(name="help", description="How to use DA VOUCHER")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="🤖 DA VOUCHER Help", color=discord.Color.blurple())
    embed.add_field(name="Trading", value="`/trade @user` → open a ticket\nPartner Accepts → Trade → both Confirm", inline=False)
    embed.add_field(name="Vouching", value="`/vouch @user trade_id stars` (Trade must be completed)", inline=False)
    embed.add_field(name="Profiles", value="`/embark Name#1234` → save your in-game ID", inline=False)
    embed.add_field(name="Rep", value="`/rep @user` (private) • `/toptraders` (public)", inline=False)
    embed.add_field(name="History", value="`/trade_history @user` → last 5 trades (private)", inline=False)
    embed.set_footer(text="Trade at your own risk • Staff can force close trades")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---- Embark ID ----
@bot.tree.command(name="embark", description="Set your Embark ID (example: Name#1234)")
@app_commands.describe(embark_id="Example: RaiderName#1234")
async def embark(interaction: discord.Interaction, embark_id: str):
    embark_id = embark_id.strip()
    if "#" not in embark_id:
        return await interaction.response.send_message("Use format like: `Name#1234`", ephemeral=True)

    name, tag = embark_id.split("#", 1)
    if not name or not tag.isdigit() or not (3 <= len(tag) <= 6):
        return await interaction.response.send_message("Use format like: `Name#1234` (numbers after #).", ephemeral=True)

    if len(name) > 20:
        return await interaction.response.send_message("Name part is too long. Keep it under ~20 chars.", ephemeral=True)

    set_embark_id(interaction.guild.id, interaction.user.id, f"{name}#{tag}")
    await interaction.response.send_message(f"✅ Saved your Embark ID as **`{name}#{tag}`**", ephemeral=True)

# ---- Trade command ----
@bot.tree.command(name="trade", description="Open a trade ticket with another user (posts in Trade Channel)")
async def trade(interaction: discord.Interaction, user: discord.Member):
    if user.bot:
        return await interaction.response.send_message("You can’t open trades with bots.", ephemeral=True)
    if user.id == interaction.user.id:
        return await interaction.response.send_message("You can’t open a trade with yourself.", ephemeral=True)

    cfg = get_config(interaction.guild.id)
    trade_channel_id = int(cfg["trade_channel_id"] or 0)
    if not trade_channel_id:
        return await interaction.response.send_message(
            "Trade channel isn’t set yet. Admins: use `/set_trade_channel`.",
            ephemeral=True
        )

    trade_channel = interaction.guild.get_channel(trade_channel_id)
    if not isinstance(trade_channel, discord.TextChannel):
        return await interaction.response.send_message(
            "Trade channel is invalid. Admins: run `/set_trade_channel` again.",
            ephemeral=True
        )

    trade_id = create_trade(interaction.guild.id, interaction.user.id, user.id)
    embed = build_trade_embed(interaction.guild, trade_id)
    view = PendingTradeView()

    await interaction.response.send_message(
        f"Trade ticket created ✅ Posted in {trade_channel.mention}\nTrade ID: `{trade_id}`",
        ephemeral=True
    )
    # Soft Embark-ID nudge (does NOT block trading)
    opener_eid = get_embark_id(interaction.guild.id, interaction.user.id)
    partner_eid = get_embark_id(interaction.guild.id, user.id)

    tip_lines = []
    if not opener_eid:
        tip_lines.append("• You don’t have an Embark ID set. Use **`/embark Name#1234`**.")
    if not partner_eid:
        tip_lines.append(f"• {user.mention} doesn’t have an Embark ID set yet (it will show **Not set**).")

    if tip_lines:
        await interaction.followup.send(
            "🧾 **Trade Tip**\n" + "\n".join(tip_lines) +
            "\nSetting it makes adding each other in-game way faster ✅",
            ephemeral=True
        )

    msg = await trade_channel.send(content=f"{user.mention}", embed=embed, view=view)
    set_trade_message(trade_id, trade_channel.id, msg.id)

# ---- Trade History ----
@bot.tree.command(name="trade_history", description="Show a user's last 5 trades (private)")
async def trade_history(interaction: discord.Interaction, user: discord.Member):
    rows = last_trades_for_user(interaction.guild.id, user.id, limit=5)
    if not rows:
        return await interaction.response.send_message("No trades found for that user yet.", ephemeral=True)

    lines = []
    for r in rows:
        opener_id = int(r["opener_id"])
        partner_id = int(r["partner_id"])
        other_id = partner_id if user.id == opener_id else opener_id
        other = interaction.guild.get_member(other_id)
        other_txt = other.mention if other else f"<@{other_id}>"
        date_txt = time.strftime("%Y-%m-%d", time.localtime(int(r["created_at"])))
        lines.append(f"`{r['trade_id']}` • **{str(r['status']).title()}** • with {other_txt} • {date_txt}")

    embed = discord.Embed(
        title=f"🗂️ Trade History — {user.display_name}",
        description="\n".join(lines),
        color=discord.Color.blurple()
    )
    embed.set_footer(text="Shows last 5 trades (any status)")
    await interaction.response.send_message(embed=embed, ephemeral=True)
# ---- Report: add extra user (staff) ----
@bot.tree.command(name="report_add_user", description="Staff: add another user to the current report channel")
async def report_add_user(interaction: discord.Interaction, user: discord.Member):
    if not isinstance(interaction.user, discord.Member) or not is_staff_member(interaction.user):
        return await interaction.response.send_message("Staff only.", ephemeral=True)

    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message("Use this inside a report text channel.", ephemeral=True)

    report_id = _parse_report_id_from_channel(channel)
    if not report_id:
        return await interaction.response.send_message("This channel doesn't look like a report channel.", ephemeral=True)

    overwrite = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
    await channel.set_permissions(user, overwrite=overwrite, reason=f"Added to report {report_id} by staff")
    await interaction.response.send_message(f"✅ Added {user.mention} to this report channel.", ephemeral=True)



# ---- Vouch (trade_id + stars REQUIRED) ----
@bot.tree.command(name="vouch", description="Leave a vouch (requires completed Trade ID)")
@app_commands.describe(
    user="Who are you vouching for?",
    trade_id="Trade ID (must be completed)",
    stars="1-5 star rating (required)",
    note="Short note (optional)",
    proof_url="Link to proof (optional)"
)
async def vouch(
    interaction: discord.Interaction,
    user: discord.Member,
    trade_id: str,
    stars: app_commands.Range[int, 1, 5],
    note: Optional[str] = None,
    proof_url: Optional[str] = None
):
    if user.bot:
        return await interaction.response.send_message("You can’t vouch for bots.", ephemeral=True)
    if user.id == interaction.user.id:
        return await interaction.response.send_message("You can’t vouch for yourself.", ephemeral=True)

    cfg = get_config(interaction.guild.id)
    vouch_channel_id = int(cfg["vouch_channel_id"] or 0)
    if not vouch_channel_id:
        return await interaction.response.send_message(
            "Vouch channel isn’t set yet. Admins: use `/set_vouch_channel`.",
            ephemeral=True
        )

    vouch_channel = interaction.guild.get_channel(vouch_channel_id)
    if not isinstance(vouch_channel, discord.TextChannel):
        return await interaction.response.send_message(
            "Vouch channel is invalid. Admins: run `/set_vouch_channel` again.",
            ephemeral=True
        )

    trade_id = trade_id.strip().upper()
    trade_row = get_trade(trade_id)
    if not trade_row:
        return await interaction.response.send_message("That Trade ID doesn’t exist.", ephemeral=True)

    if int(trade_row["guild_id"]) != interaction.guild.id:
        return await interaction.response.send_message("That Trade ID is not for this server.", ephemeral=True)

    if trade_row["status"] != "completed":
        return await interaction.response.send_message("That trade is not completed yet.", ephemeral=True)

    opener_id = int(trade_row["opener_id"])
    partner_id = int(trade_row["partner_id"])
    voucher_id = interaction.user.id
    target_id = user.id

    if voucher_id not in (opener_id, partner_id):
        return await interaction.response.send_message("Only trade participants can vouch for that trade.", ephemeral=True)

    if {voucher_id, target_id} != {opener_id, partner_id}:
        return await interaction.response.send_message("You must vouch for the other person in that Trade ID.", ephemeral=True)

    try:
        add_vouch(interaction.guild.id, trade_id, target_id, voucher_id, int(stars), note, proof_url)
    except sqlite3.IntegrityError:
        return await interaction.response.send_message("You already vouched for this trade.", ephemeral=True)

    total = vouch_count(interaction.guild.id, target_id)
    avg = avg_stars(interaction.guild.id, target_id)

    tier_update = None
    target_member = interaction.guild.get_member(target_id)
    if target_member:
        tier_update = await apply_roles(target_member, total)

    embed = build_vouch_embed(
        interaction.guild,
        trade_id,
        trader=target_member or user,
        voucher=interaction.user,
        stars=int(stars),
        total=total,
        avg=avg,
        tier_update=tier_update,
        note=note,
        proof_url=proof_url
    )

    await interaction.response.send_message(f"Logged ✅ Posted in {vouch_channel.mention}.", ephemeral=True)
    await vouch_channel.send(embed=embed)

# ---- Rep stays private ----
@bot.tree.command(name="rep", description="Check a user's vouch count and tier (private)")
async def rep(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    user = user or interaction.user
    gid = interaction.guild.id

    total = vouch_count(gid, user.id)
    avg = avg_stars(gid, user.id)
    eid = get_embark_id(gid, user.id)

    cfg = get_config(gid)
    tier = trader_tier_label(user if isinstance(user, discord.Member) else None, total, cfg)

    badges = user_badges(user if isinstance(user, discord.Member) else None)

    embed = discord.Embed(title="📈 Trader Rep", color=discord.Color.blurple())
    embed.add_field(name="User", value=user.mention, inline=True)
    embed.add_field(name="Embark ID", value=f"`{eid}`" if eid else "*Not set*", inline=True)
    embed.add_field(name="Trader Tier", value=tier, inline=True)

    embed.add_field(name="Vouches", value=str(total), inline=True)
    embed.add_field(name="Avg Rating", value=f"{avg:.2f}/5 ⭐", inline=True)

    if badges["region"] or badges["platform"]:
        embed.add_field(
            name="Info",
            value=f"{badges['region'] or '*No region*'} • {badges['platform'] or '*No platform*'}",
            inline=False
        )

    if badges["playstyle"]:
        embed.add_field(name="Raider Type", value=" • ".join(badges["playstyle"]), inline=False)

    if badges["staff"]:
        embed.add_field(name="Staff", value=badges["staff"], inline=False)

    embed.set_footer(text="Private • Vouches require completed trades (Trade ID).")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="stats", description="Public trader stats & success rate")
async def stats_cmd(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    user = user or interaction.user
    gid = interaction.guild.id

    # Vouch stats
    total_vouches = vouch_count(gid, user.id)
    avg_rating = avg_stars(gid, user.id)

    # Trade stats
    tstats = trade_stats_for_user(gid, user.id)
    total_trades = tstats["total"]
    completed = tstats["completed"]
    failed = tstats["failed"]
    success_rate = (completed / total_trades * 100) if total_trades > 0 else 0

    # Recent trades
    recent = last_trades_for_user(gid, user.id, limit=3)
    recent_lines = []
    for r in recent:
        opener_id = int(r["opener_id"])
        partner_id = int(r["partner_id"])
        other_id = partner_id if user.id == opener_id else opener_id
        other = interaction.guild.get_member(other_id)
        other_txt = other.mention if other else f"<@{other_id}>"
        recent_lines.append(f"`{r['trade_id']}` • **{str(r['status']).title()}** • with {other_txt}")

    eid = get_embark_id(gid, user.id)
    cfg = get_config(gid)
    tier = trader_tier_label(user if isinstance(user, discord.Member) else None, total_vouches, cfg)
    badges = user_badges(user if isinstance(user, discord.Member) else None)

    embed = discord.Embed(
        title=f"📊 Trader Stats — {user.display_name}",
        color=discord.Color.blurple()
    )

    embed.add_field(name="Trader Tier", value=tier, inline=True)
    embed.add_field(name="Embark ID", value=f"`{eid}`" if eid else "*Not set*", inline=True)
    embed.add_field(name="Vouches", value=f"{total_vouches} • {avg_rating:.2f}/5 ⭐", inline=True)

    if badges["region"] or badges["platform"]:
        embed.add_field(
            name="Region / Platform",
            value=f"{badges['region'] or '*No region*'} • {badges['platform'] or '*No platform*'}",
            inline=False
        )

    if badges["playstyle"]:
        embed.add_field(name="Raider Type", value=" • ".join(badges["playstyle"]), inline=False)

    if badges["staff"]:
        embed.add_field(name="Staff", value=badges["staff"], inline=False)

    embed.add_field(
        name="🤝 Trade Activity",
        value=(
            f"• Total Trades: **{total_trades}**\n"
            f"• Completed Trades: **{completed}**\n"
            f"• Cancelled/Expired: **{failed}**\n"
            f"• Success Rate: **{success_rate:.0f}%**"
        ),
        inline=False
    )

    if recent_lines:
        embed.add_field(name="🗂 Recent Activity", value="\n".join(recent_lines), inline=False)

    embed.set_footer(text="Public stats • Based on tracked trade tickets")
    await interaction.response.send_message(embed=embed, ephemeral=False)

@bot.tree.command(name="toptraders", description="Show top traders (public)")
async def toptraders_cmd(interaction: discord.Interaction):
    gid = interaction.guild.id
    cfg = get_config(gid)

    top = top_traders(gid, limit=10)
    if not top:
        return await interaction.response.send_message("No vouches yet.", ephemeral=True)

    lines_out = []
    for i, (uid, v, a) in enumerate(top, start=1):
        member = interaction.guild.get_member(uid)
        name = member.mention if member else f"<@{uid}>"

        # Leaderboard extras: ONLY Region + Platform + Trader Tier
        tier = trader_tier_label(member, v, cfg)
        badges = user_badges(member)
        tags = [t for t in [tier, badges["region"], badges["platform"]] if t and t != "Unranked"]

        tag_txt = f" • {' • '.join(tags)}" if tags else ""
        lines_out.append(f"**#{i}** {name} — **{v}** vouches — **{a:.2f}/5** ⭐{tag_txt}")

    embed = discord.Embed(title="🏆 Top Traders", description="\n".join(lines_out), color=discord.Color.gold())
    embed.set_footer(text="Ranked by vouches • Tie-breaker: avg rating")
    await interaction.response.send_message(embed=embed, ephemeral=False)

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable")

bot.run(TOKEN)
