import os
import time
import sqlite3
import logging
import random
import string
from dataclasses import dataclass
from typing import Optional, List

import discord
from discord import app_commands
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional but recommended for fast slash commands

logging.basicConfig(level=logging.INFO)

DB_PATH = "vouchbot.db"
TRADE_EXPIRE_SECONDS = 3 * 60 * 60  # 3 hours

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

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

        # Vouches (now: trade_id required for real vouches, stars required)
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

        # --- Safe migrations for your existing DB ---
        # Add trade_channel_id to config (if old DB)
        try:
            con.execute("ALTER TABLE guild_config ADD COLUMN trade_channel_id INTEGER")
        except sqlite3.OperationalError:
            pass

        # Add new columns to vouches (if old DB)
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

    if status == "pending":
        embed.add_field(name="Status", value="Waiting for partner to accept or decline.", inline=False)
        embed.set_footer(text="Partner: click Accept/Decline • Auto-expires in 3 hours")
    elif status == "active":
        embed.add_field(name="Status", value="Active — complete the trade then both confirm.", inline=False)
        embed.add_field(name="Confirmations", value=f"Opener: {'✅' if oc else '⏳'} • Partner: {'✅' if pc else '⏳'}", inline=False)
        embed.set_footer(text="Only the opener/partner can confirm • Auto-expires in 3 hours")
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

def build_vouch_embed(guild: discord.Guild, trade_id: str, trader: discord.Member, voucher: discord.Member,
                     stars: int, total: int, avg: float, tier_update: Optional[str],
                     note: Optional[str], proof_url: Optional[str]) -> discord.Embed:
    star_line = "⭐" * stars + "☆" * (5 - stars)

    embed = discord.Embed(
        title="✅ Vouch Logged",
        description=f"{star_line}  **({stars}/5)**",
        color=discord.Color.green()
    )
    embed.add_field(name="Trade ID", value=f"`{trade_id}`", inline=False)
    embed.add_field(name="Trader", value=trader.mention, inline=True)
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

# -------------------- Trade Views (Buttons) --------------------
class PendingTradeView(discord.ui.View):
    def __init__(self, trade_id: str):
        super().__init__(timeout=None)
        self.trade_id = trade_id

    @discord.ui.button(label="Accept Trade", style=discord.ButtonStyle.success, custom_id="trade_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        trade = get_trade(self.trade_id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)
        if trade["status"] != "pending":
            return await interaction.response.send_message("This trade is no longer pending.", ephemeral=True)

        if interaction.user.id != int(trade["partner_id"]):
            return await interaction.response.send_message("Only the tagged partner can accept.", ephemeral=True)

        update_trade(self.trade_id, status="active", accepted=1)

        embed = build_trade_embed(interaction.guild, self.trade_id)
        await interaction.response.edit_message(embed=embed, view=ActiveTradeView(self.trade_id))

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, custom_id="trade_decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        trade = get_trade(self.trade_id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)
        if trade["status"] != "pending":
            return await interaction.response.send_message("This trade is no longer pending.", ephemeral=True)

        if interaction.user.id != int(trade["partner_id"]):
            return await interaction.response.send_message("Only the tagged partner can decline.", ephemeral=True)

        update_trade(self.trade_id, status="declined")
        embed = build_trade_embed(interaction.guild, self.trade_id)
        await interaction.response.edit_message(embed=embed, view=None)

class ActiveTradeView(discord.ui.View):
    def __init__(self, trade_id: str):
        super().__init__(timeout=None)
        self.trade_id = trade_id

    @discord.ui.button(label="Confirm Complete (Opener)", style=discord.ButtonStyle.primary, custom_id="trade_confirm_opener")
    async def confirm_opener(self, interaction: discord.Interaction, button: discord.ui.Button):
        trade = get_trade(self.trade_id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)
        if trade["status"] != "active":
            return await interaction.response.send_message("Trade is not active.", ephemeral=True)
        if interaction.user.id != int(trade["opener_id"]):
            return await interaction.response.send_message("Only the opener can press this.", ephemeral=True)

        update_trade(self.trade_id, opener_confirmed=1)
        await self._refresh_or_finalize(interaction)

    @discord.ui.button(label="Confirm Complete (Partner)", style=discord.ButtonStyle.primary, custom_id="trade_confirm_partner")
    async def confirm_partner(self, interaction: discord.Interaction, button: discord.ui.Button):
        trade = get_trade(self.trade_id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)
        if trade["status"] != "active":
            return await interaction.response.send_message("Trade is not active.", ephemeral=True)
        if interaction.user.id != int(trade["partner_id"]):
            return await interaction.response.send_message("Only the partner can press this.", ephemeral=True)

        update_trade(self.trade_id, partner_confirmed=1)
        await self._refresh_or_finalize(interaction)

    async def _refresh_or_finalize(self, interaction: discord.Interaction):
        trade = get_trade(self.trade_id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)

        if int(trade["opener_confirmed"]) == 1 and int(trade["partner_confirmed"]) == 1:
            update_trade(self.trade_id, status="completed")
            embed = build_trade_embed(interaction.guild, self.trade_id)
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            embed = build_trade_embed(interaction.guild, self.trade_id)
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

# -------------------- Commands --------------------
def admin_only(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.administrator

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

    # Persistent views (important for button reliability across restarts)
    # NOTE: These are per-message, so the buttons still work; for old messages after restart,
    # discord.py needs registered views. We'll register generic views by custom_id usage.
    # This is enough since our custom_id values are static.
    bot.add_view(PendingTradeView("DUMMY"))
    bot.add_view(ActiveTradeView("DUMMY"))

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
    view = PendingTradeView(trade_id)

    await interaction.response.send_message(
        f"Trade ticket created ✅ Posted in {trade_channel.mention}\nTrade ID: `{trade_id}`",
        ephemeral=True
    )

    msg = await trade_channel.send(content=f"{user.mention}", embed=embed, view=view)
    set_trade_message(trade_id, trade_channel.id, msg.id)

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

    # Must vouch for the OTHER participant
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

    cfg = get_config(gid)
    tiers = get_tiers(cfg)

    achieved = "Unranked"
    for t in tiers:
        if t.role_id and total >= t.threshold:
            achieved = t.name
            break

    embed = discord.Embed(title="📈 Trader Rep", color=discord.Color.blurple())
    embed.add_field(name="User", value=user.mention, inline=True)
    embed.add_field(name="Vouches", value=str(total), inline=True)
    embed.add_field(name="Avg Rating", value=f"{avg:.2f}/5 ⭐", inline=True)
    embed.add_field(name="Tier", value=achieved, inline=True)
    embed.set_footer(text="Vouches require completed trades (Trade ID).")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---- Leaderboard (public) ----
@bot.tree.command(name="toptraders", description="Show top traders (public)")
async def toptraders_cmd(interaction: discord.Interaction):
    gid = interaction.guild.id
    top = top_traders(gid, limit=10)
    if not top:
        return await interaction.response.send_message("No vouches yet.", ephemeral=True)

    lines = []
    for i, (uid, v, a) in enumerate(top, start=1):
        member = interaction.guild.get_member(uid)
        name = member.mention if member else f"<@{uid}>"
        lines.append(f"**#{i}** {name} — **{v}** vouches — **{a:.2f}/5** ⭐")

    embed = discord.Embed(title="🏆 Top Traders", description="\n".join(lines), color=discord.Color.gold())
    embed.set_footer(text="Ranked by vouches • Tie-breaker: avg rating")
    await interaction.response.send_message(embed=embed, ephemeral=False)

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable")

bot.run(TOKEN)
