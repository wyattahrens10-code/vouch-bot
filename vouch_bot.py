import os
import time
import sqlite3
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional but recommended for fast slash commands

logging.basicConfig(level=logging.INFO)

DB_PATH = "vouchbot.db"
COOLDOWN_SECONDS = 24 * 60 * 60  # 24h per voucher->target

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------- DB --------------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER PRIMARY KEY,
            vouch_channel_id INTEGER,
            role_new_id INTEGER,
            role_verified_id INTEGER,
            role_trusted_id INTEGER,
            thresh_new INTEGER DEFAULT 1,
            thresh_verified INTEGER DEFAULT 5,
            thresh_trusted INTEGER DEFAULT 15
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS vouches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            voucher_id INTEGER NOT NULL,
            note TEXT,
            proof_url TEXT,
            created_at INTEGER NOT NULL
        )
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

def add_vouch(guild_id: int, target_id: int, voucher_id: int, note: Optional[str], proof_url: Optional[str]) -> None:
    now = int(time.time())
    with db() as con:
        con.execute(
            "INSERT INTO vouches (guild_id, target_id, voucher_id, note, proof_url, created_at) VALUES (?,?,?,?,?,?)",
            (guild_id, target_id, voucher_id, note, proof_url, now)
        )
        con.commit()

def vouch_count(guild_id: int, target_id: int) -> int:
    with db() as con:
        row = con.execute(
            "SELECT COUNT(*) AS c FROM vouches WHERE guild_id = ? AND target_id = ?",
            (guild_id, target_id)
        ).fetchone()
        return int(row["c"])

def can_vouch_now(guild_id: int, target_id: int, voucher_id: int) -> Tuple[bool, int]:
    now = int(time.time())
    cutoff = now - COOLDOWN_SECONDS
    with db() as con:
        row = con.execute(
            """
            SELECT created_at FROM vouches
            WHERE guild_id = ? AND target_id = ? AND voucher_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (guild_id, target_id, voucher_id)
        ).fetchone()
        if not row:
            return True, 0
        last = int(row["created_at"])
        if last >= cutoff:
            remaining = (last + COOLDOWN_SECONDS) - now
            return False, max(0, remaining)
        return True, 0

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

def pretty_time(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"

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

@bot.tree.command(name="set_vouch_channel", description="Admin: set a channel where vouches get logged (optional)")
async def set_vouch_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not admin_only(interaction):
        return await interaction.response.send_message("Admin only.", ephemeral=True)

    gid = interaction.guild.id
    set_config_value(gid, "vouch_channel_id", channel.id)
    await interaction.response.send_message(f"Vouch log channel set to {channel.mention}.", ephemeral=True)

@bot.tree.command(name="vouch", description="Give a vouch to a user (24h cooldown per person)")
async def vouch(interaction: discord.Interaction, user: discord.Member, note: Optional[str] = None, proof_url: Optional[str] = None):
    if user.bot:
        return await interaction.response.send_message("You can’t vouch for bots.", ephemeral=True)
    if user.id == interaction.user.id:
        return await interaction.response.send_message("You can’t vouch for yourself.", ephemeral=True)

    gid = interaction.guild.id
    ok, remaining = can_vouch_now(gid, user.id, interaction.user.id)
    if not ok:
        return await interaction.response.send_message(
            f"You already vouched for {user.mention} recently. Try again in **{pretty_time(remaining)}**.",
            ephemeral=True
        )

    add_vouch(gid, user.id, interaction.user.id, note, proof_url)
    total = vouch_count(gid, user.id)
    new_tier = await apply_roles(user, total)

    await interaction.response.send_message(
        f"✅ Vouch added for {user.mention}. They now have **{total}** vouch(es).",
        ephemeral=True
    )

    cfg = get_config(gid)
    log_channel_id = int(cfg["vouch_channel_id"] or 0)
    if log_channel_id:
        ch = interaction.guild.get_channel(log_channel_id)
        if isinstance(ch, discord.TextChannel):
            embed = discord.Embed(title="✅ New Vouch", color=discord.Color.green())
            embed.add_field(name="Trader", value=user.mention, inline=True)
            embed.add_field(name="Vouched By", value=interaction.user.mention, inline=True)
            embed.add_field(name="Total Vouches", value=str(total), inline=True)
            if new_tier:
                embed.add_field(name="Tier Update", value=f"Now: **{new_tier}**", inline=False)
            if note:
                embed.add_field(name="Note", value=note[:1024], inline=False)
            if proof_url:
                embed.add_field(name="Proof", value=proof_url[:1024], inline=False)
            await ch.send(embed=embed)

@bot.tree.command(name="rep", description="Check a user's vouch count and tier")
async def rep(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    user = user or interaction.user
    gid = interaction.guild.id
    total = vouch_count(gid, user.id)
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
    embed.add_field(name="Tier", value=achieved, inline=True)
    embed.set_footer(text="Vouch cooldown: 24h per person.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable")

bot.run(TOKEN)
