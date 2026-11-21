from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# Expected layout:
# ~/arabi-vps/
#   manager.sh
#   instances/<vps-name>/config
#   discord-bot/bot.py
ROOT = Path.home() / "arabi-vps"
BASE = Path(__file__).resolve().parent
MANAGER = ROOT / "manager.sh"
INSTANCES = ROOT / "instances"
DB_PATH = BASE / "arabi.db"

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
DEFAULT_CHANNEL_ID = int(os.getenv("DEFAULT_CHANNEL_ID", "0") or "0")
NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")

START_TIME = time.time()
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db() -> None:
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS access (
                user_id INTEGER NOT NULL,
                vps TEXT NOT NULL,
                PRIMARY KEY (user_id, vps)
            )
        """)


def is_owner(user: discord.abc.User) -> bool:
    return OWNER_ID != 0 and user.id == OWNER_ID


def is_admin(user: discord.abc.User) -> bool:
    if is_owner(user):
        return True
    with db() as con:
        return con.execute(
            "SELECT 1 FROM admins WHERE user_id = ?", (user.id,)
        ).fetchone() is not None


def has_access(user: discord.abc.User, vps_name: str) -> bool:
    if is_admin(user):
        return True
    with db() as con:
        return con.execute(
            "SELECT 1 FROM access WHERE user_id = ? AND vps = ?",
            (user.id, vps_name),
        ).fetchone() is not None


def vps_names() -> list[str]:
    if not INSTANCES.is_dir():
        return []
    result = []
    for directory in INSTANCES.iterdir():
        if (
            directory.is_dir()
            and (directory / "config").is_file()
            and NAME_RE.fullmatch(directory.name)
        ):
            result.append(directory.name)
    return sorted(result)


def read_vps_config(vps_name: str) -> dict[str, str]:
    if vps_name not in vps_names():
        raise ValueError("VPS does not exist.")
    data: dict[str, str] = {}
    config_path = INSTANCES / vps_name / "config"
    for line in config_path.read_text(errors="replace").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip().strip('"').strip("'")
    return data


async def run_manager(action: str, *args: str) -> str:
    allowed = {"list", "create", "start", "stop", "restart", "delete"}
    if action not in allowed:
        raise ValueError("Invalid manager action.")
    if not MANAGER.is_file():
        raise RuntimeError(f"Manager script not found: {MANAGER}")

    if action in {"start", "stop", "restart", "delete"}:
        if not args or args[0] not in vps_names():
            raise ValueError("VPS not found.")

    if action == "create":
        if len(args) != 4:
            raise ValueError("Usage: create <name> <RAM_MB> <CPU> <DISK_GB>")
        name, ram, cpu, disk = args
        if not NAME_RE.fullmatch(name):
            raise ValueError("VPS name must use 1-32 letters, numbers, _ or -.")
        if name in vps_names():
            raise ValueError("That VPS name already exists.")
        if not all(value.isdigit() for value in (ram, cpu, disk)):
            raise ValueError("RAM, CPU and disk must be whole numbers.")
        ram_i, cpu_i, disk_i = map(int, (ram, cpu, disk))
        if not (512 <= ram_i <= 262144):
            raise ValueError("RAM must be between 512 and 262144 MB.")
        if not (1 <= cpu_i <= 64):
            raise ValueError("CPU must be between 1 and 64 cores.")
        if not (3 <= disk_i <= 2048):
            raise ValueError("Disk must be between 3 and 2048 GB.")

    proc = await asyncio.create_subprocess_exec(
        "bash", str(MANAGER), action, *args,
        cwd=str(ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("Manager operation timed out after 180 seconds.")

    result = output.decode(errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(result[-2800:] or "Manager operation failed.")
    return result[-2800:] or "Operation completed."


def make_embed(title: str, description: str, color: int = 0x2878FF) -> discord.Embed:
    embed = discord.Embed(
        title=f"◆ ARABI VPS | {title}",
        description=description,
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="ARABI VPS • Virtual Server Management")
    return embed


def choices_for(user: discord.abc.User, current: str = "") -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=name, value=name)
        for name in vps_names()
        if current.lower() in name.lower() and has_access(user, name)
    ][:25]


async def require_admin(interaction: discord.Interaction) -> bool:
    if not is_admin(interaction.user):
        await interaction.response.send_message(
            "⛔ You do not have permission.", ephemeral=True
        )
        return False
    return True


async def do_vps_operation(
    interaction: discord.Interaction, action: str, vps_name: str
) -> None:
    if not has_access(interaction.user, vps_name):
        await interaction.response.send_message(
            "⛔ You do not have access to this VPS.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    try:
        result = await run_manager(action, vps_name)
        await interaction.followup.send(
            embed=make_embed(action.upper(), f"```text\n{result[:2800]}\n```", 0x16A36A),
            ephemeral=True,
        )
    except Exception as exc:
        await interaction.followup.send(
            embed=make_embed(
                "OPERATION FAILED", f"```text\n{str(exc)[:2500]}\n```", 0xD83C3E
            ),
            ephemeral=True,
        )


class CreateModal(discord.ui.Modal, title="Create New VPS"):
    name = discord.ui.TextInput(label="VPS Name", placeholder="my-vps", max_length=32)
    ram = discord.ui.TextInput(label="RAM (MB)", placeholder="2048")
    cpu = discord.ui.TextInput(label="CPU Cores", placeholder="2")
    disk = discord.ui.TextInput(label="Disk (GB)", placeholder="20")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "⛔ You do not have permission.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        name, ram, cpu, disk = (
            str(self.name).strip(),
            str(self.ram).strip(),
            str(self.cpu).strip(),
            str(self.disk).strip(),
        )
        try:
            result = await run_manager("create", name, ram, cpu, disk)
            with db() as con:
                con.execute(
                    "INSERT OR IGNORE INTO access(user_id, vps) VALUES(?, ?)",
                    (interaction.user.id, name),
                )
            await interaction.followup.send(
                embed=make_embed(
                    "VPS CREATED",
                    f"**Name:** `{name}`\n**RAM:** `{ram} MB`\n"
                    f"**CPU:** `{cpu}`\n**Disk:** `{disk} GB`\n\n"
                    f"```text\n{result[-1400:]}\n```",
                    0x16A36A,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(
                embed=make_embed(
                    "CREATE FAILED", f"```text\n{str(exc)[:2000]}\n```", 0xD83C3E
                ),
                ephemeral=True,
            )


class ManageView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=180)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This control panel belongs to another admin.", ephemeral=True
            )
            return False
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "⛔ You do not have permission.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Create VPS", style=discord.ButtonStyle.success, row=0)
    async def create_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(CreateModal())

    @discord.ui.button(label="VPS List", style=discord.ButtonStyle.primary, row=0)
    async def list_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            result = await run_manager("list")
            await interaction.followup.send(
                embed=make_embed("VPS INVENTORY", f"```text\n{result[:2800]}\n```"),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(str(exc)[:1900], ephemeral=True)

    @discord.ui.button(label="Manage Users", style=discord.ButtonStyle.secondary, row=1)
    async def users_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        with db() as con:
            admins = con.execute(
                "SELECT user_id FROM admins ORDER BY user_id"
            ).fetchall()
            grants = con.execute(
                "SELECT user_id, vps FROM access ORDER BY user_id, vps"
            ).fetchall()
        lines = ["**Admins**"]
        lines.extend(f"• <@{row['user_id']}>" for row in admins)
        if not admins:
            lines.append("• None")
        lines.extend(["", "**VPS access assignments**"])
        lines.extend(
            f"• <@{row['user_id']}> → `{row['vps']}`" for row in grants
        )
        if not grants:
            lines.append("• None")
        await interaction.response.send_message(
            embed=make_embed("USER ACCESS", "\n".join(lines)), ephemeral=True
        )


@bot.event
async def on_ready() -> None:
    try:
        synced = await bot.tree.sync()
        print(f"ARABI VPS ONLINE: {bot.user} | {len(synced)} slash commands synced")
    except Exception as exc:
        print("Command sync error:", repr(exc))


@bot.tree.command(name="manage", description="Open the ARABI VPS management panel")
async def manage(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await interaction.response.send_message(
        embed=make_embed(
            "MANAGEMENT CONSOLE",
            "Welcome to your VPS control center.\n\n"
            "➕ Create virtual servers\n"
            "🖥️ View VPS inventory\n"
            "👥 Manage users and permissions\n"
            "🔐 Control VPS access",
        ),
        view=ManageView(interaction.user.id),
        ephemeral=True,
    )


@bot.tree.command(name="vps", description="Show your assigned VPS")
async def vps(interaction: discord.Interaction) -> None:
    with db() as con:
        rows = con.execute(
            "SELECT vps FROM access WHERE user_id = ? ORDER BY vps",
            (interaction.user.id,),
        ).fetchall()
    assigned = [row["vps"] for row in rows if row["vps"] in vps_names()]
    if is_admin(interaction.user):
        assigned = vps_names()
    description = "\n".join(f"• `{name}`" for name in assigned) or "No VPS assigned."
    await interaction.response.send_message(
        embed=make_embed("YOUR VPS", description), ephemeral=True
    )


@bot.tree.command(name="vpsinfo", description="Show information about an assigned VPS")
@app_commands.autocomplete(vps_name=lambda i, current: choices_for(i.user, current))
async def vpsinfo(interaction: discord.Interaction, vps_name: str) -> None:
    if not has_access(interaction.user, vps_name):
        await interaction.response.send_message("⛔ No access to this VPS.", ephemeral=True)
        return
    try:
        cfg = read_vps_config(vps_name)
        running = (INSTANCES / vps_name / "pid").exists()
        description = (
            f"**Name:** `{vps_name}`\n"
            f"**RAM:** `{cfg.get('RAM', 'Unknown')} MB`\n"
            f"**CPU:** `{cfg.get('CPU', 'Unknown')}`\n"
            f"**Disk:** `{cfg.get('DISK', 'Unknown')} GB`\n"
            f"**SSH Port:** `{cfg.get('PORT', 'Unknown')}`\n"
            f"**State marker:** `{'PID file exists' if running else 'No PID file'}`"
        )
        await interaction.response.send_message(
            embed=make_embed("VPS DETAILS", description), ephemeral=True
        )
    except Exception as exc:
        await interaction.response.send_message(str(exc)[:1900], ephemeral=True)


@bot.tree.command(name="vpsstart", description="Start an assigned VPS")
@app_commands.autocomplete(vps_name=lambda i, current: choices_for(i.user, current))
async def vpsstart(interaction: discord.Interaction, vps_name: str) -> None:
    await do_vps_operation(interaction, "start", vps_name)


@bot.tree.command(name="vpsstop", description="Stop an assigned VPS")
@app_commands.autocomplete(vps_name=lambda i, current: choices_for(i.user, current))
async def vpsstop(interaction: discord.Interaction, vps_name: str) -> None:
    await do_vps_operation(interaction, "stop", vps_name)


@bot.tree.command(name="vpsrestart", description="Restart an assigned VPS")
@app_commands.autocomplete(vps_name=lambda i, current: choices_for(i.user, current))
async def vpsrestart(interaction: discord.Interaction, vps_name: str) -> None:
    await do_vps_operation(interaction, "restart", vps_name)


@bot.tree.command(name="grant", description="Give a user access to a VPS (admin)")
@app_commands.autocomplete(vps_name=lambda i, current: [
    app_commands.Choice(name=n, value=n)
    for n in vps_names() if current.lower() in n.lower()
][:25])
async def grant(
    interaction: discord.Interaction, user: discord.User, vps_name: str
) -> None:
    if not await require_admin(interaction):
        return
    if vps_name not in vps_names():
        await interaction.response.send_message("VPS not found.", ephemeral=True)
        return
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO access(user_id, vps) VALUES(?, ?)",
            (user.id, vps_name),
        )
    await interaction.response.send_message(
        f"✅ Granted `{vps_name}` access to {user.mention}.", ephemeral=True
    )


@bot.tree.command(name="revoke", description="Remove a user's VPS access (admin)")
@app_commands.autocomplete(vps_name=lambda i, current: [
    app_commands.Choice(name=n, value=n)
    for n in vps_names() if current.lower() in n.lower()
][:25])
async def revoke(
    interaction: discord.Interaction, user: discord.User, vps_name: str
) -> None:
    if not await require_admin(interaction):
        return
    with db() as con:
        cursor = con.execute(
            "DELETE FROM access WHERE user_id = ? AND vps = ?",
            (user.id, vps_name),
        )
    await interaction.response.send_message(
        "✅ Access revoked." if cursor.rowcount else "No matching access found.",
        ephemeral=True,
    )


@bot.tree.command(name="makeadmin", description="Promote a user to admin (owner only)")
async def makeadmin(interaction: discord.Interaction, user: discord.User) -> None:
    if not is_owner(interaction.user):
        await interaction.response.send_message(
            "⛔ Only the bot owner can promote admins.", ephemeral=True
        )
        return
    with db() as con:
        con.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (user.id,))
    await interaction.response.send_message(
        f"✅ {user.mention} is now an admin.", ephemeral=True
    )


@bot.tree.command(name="removeadmin", description="Remove an admin (owner only)")
async def removeadmin(interaction: discord.Interaction, user: discord.User) -> None:
    if not is_owner(interaction.user):
        await interaction.response.send_message(
            "⛔ Only the bot owner can remove admins.", ephemeral=True
        )
        return
    if user.id == OWNER_ID:
        await interaction.response.send_message(
            "The configured owner cannot be removed.", ephemeral=True
        )
        return
    with db() as con:
        con.execute("DELETE FROM admins WHERE user_id = ?", (user.id,))
    await interaction.response.send_message(
        f"✅ Admin permission removed from {user.mention}.", ephemeral=True
    )


@bot.tree.command(name="accesslist", description="Show VPS access assignments (admin)")
async def accesslist(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    with db() as con:
        rows = con.execute(
            "SELECT user_id, vps FROM access ORDER BY vps, user_id"
        ).fetchall()
    description = "\n".join(
        f"• <@{row['user_id']}> → `{row['vps']}`" for row in rows
    ) or "No assignments."
    await interaction.response.send_message(
        embed=make_embed("ACCESS LIST", description), ephemeral=True
    )


@bot.tree.command(name="botstatus", description="Show bot status")
async def botstatus(interaction: discord.Interaction) -> None:
    uptime = int(time.time() - START_TIME)
    manager_status = "Available" if MANAGER.is_file() else "Missing"
    await interaction.response.send_message(
        embed=make_embed(
            "SYSTEM STATUS",
            f"**Bot:** 🟢 Online\n"
            f"**Latency:** `{round(bot.latency * 1000)} ms`\n"
            f"**Manager:** `{manager_status}`\n"
            f"**VPS count:** `{len(vps_names())}`\n"
            f"**Uptime:** `{uptime // 3600}h {(uptime % 3600) // 60}m`",
        ),
        ephemeral=True,
    )


async def main() -> None:
    if not TOKEN or OWNER_ID == 0:
        raise SystemExit("Set DISCORD_TOKEN and OWNER_ID in discord-bot/.env first.")
    if not MANAGER.is_file():
        raise SystemExit(f"Manager not found: {MANAGER}")
    init_db()
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
