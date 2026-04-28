"""
vdmonitor_listener.py
Receives signals from vdmonitor.py and posts Discord alerts.

Run alongside your bot — it is started automatically from bot.py.

Requirements:
    pip install aiohttp
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from aiohttp import web
import discord

import streaming_monitor

# ==========================================================
#  Config — loaded from environment / .env
# ==========================================================

SECRET_TOKEN             = os.getenv("VDMONITOR_SECRET_TOKEN", "")
LISTENER_HOST            = os.getenv("VDMONITOR_HOST", "0.0.0.0")
LISTENER_PORT            = int(os.getenv("VDMONITOR_PORT", "8765"))
ALERT_THRESHOLD_MINUTES  = float(os.getenv("VDMONITOR_ALERT_MINUTES", "10"))
REPEAT_ALERT_MINUTES     = float(os.getenv("VDMONITOR_REPEAT_MINUTES", "15"))
LOG_CHANNEL_NAME         = os.getenv("STREAMING_LOG_CHANNEL", "streaming-logs")

# ==========================================================
#  Logging
# ==========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("vdmonitor_listener")

# ==========================================================
#  State
# ==========================================================

user_states: dict[str, dict] = {}
_bot = None


def set_bot(bot_instance):
    """Called from bot.py after the bot is ready."""
    global _bot
    _bot = bot_instance
    log.info("✅ Discord bot instance registered with vdmonitor_listener.")


# ==========================================================
#  Helpers
# ==========================================================

def _get_log_channel():
    """Find the log channel across all guilds."""
    if not _bot:
        return None
    for guild in _bot.guilds:
        chan = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
        if chan:
            return chan
    return None


def _find_user_id_by_name(username: str) -> int | None:
    """Match a username string to a Discord user ID in streaming_db."""
    username_lower = username.lower()
    for uid, entry in streaming_monitor.streaming_db.items():
        if entry.get("username", "").lower() == username_lower:
            return int(uid)
    return None


# ==========================================================
#  Discord Alerts
# ==========================================================

async def _post_idle_alert(username: str, idle_minutes: float):
    """Post an idle alert, with extra context if user is streaming."""
    channel = _get_log_channel()
    if not channel:
        log.warning("Log channel not found — cannot post idle alert.")
        return

    user_id      = _find_user_id_by_name(username)
    is_streaming = False
    is_in_voice  = False
    stream_duration = ""

    if user_id:
        entry        = streaming_monitor.streaming_db.get(str(user_id), {})
        is_streaming = bool(entry.get("current_start"))
        is_in_voice  = bool(entry.get("current_voice_start"))

        if is_streaming and streaming_monitor.TIMEZONE:
            now   = datetime.now(streaming_monitor.TIMEZONE)
            start = datetime.fromisoformat(entry["current_start"])
            mins  = (now - start).total_seconds() / 60
            stream_duration = streaming_monitor._format_duration(round(mins, 1))

    color = 0xE74C3C if is_streaming else 0xF39C12

    embed = discord.Embed(
        title=f"😴 {username} appears to be idle",
        color=color,
        timestamp=datetime.now().astimezone(),
    )
    embed.add_field(name="⏱️ Idle for",   value=f"{round(idle_minutes, 1)} minutes", inline=True)
    embed.add_field(name="🖥️ Streaming",  value=f"Yes — {stream_duration}" if is_streaming else "No", inline=True)
    embed.add_field(name="🔊 In Voice",   value="Yes" if is_in_voice else "No", inline=True)

    if is_streaming:
        embed.add_field(
            name="⚠️ Action needed",
            value=(
                f"**{username}** is screen sharing but has had **no keyboard or mouse "
                f"input for {round(idle_minutes, 1)} minutes**.\n"
                f"They may have stepped away from their desk."
            ),
            inline=False,
        )

    embed.set_footer(text="VD Monitor • Inactivity Detection")
    await channel.send(embed=embed)
    log.info(f"✅ Posted idle alert for {username} ({idle_minutes:.1f} min idle)")


async def _post_status_change(username: str, status: str, machine: str = ""):
    """Post a status change notification (started / stopped / active)."""
    channel = _get_log_channel()
    if not channel:
        return

    machine_text = f" from `{machine}`" if machine else ""

    messages = {
        "started": (f"🟢 **{username}** started their activity monitor{machine_text}.", 0x2ECC71),
        "stopped": (f"🔴 **{username}** stopped their activity monitor{machine_text}.",  0x95A5A6),
        "active":  (f"✅ **{username}** is active again after being idle.",               0x2ECC71),
    }

    if status not in messages:
        return

    text, color = messages[status]
    embed = discord.Embed(description=text, color=color, timestamp=datetime.now().astimezone())
    embed.set_footer(text="VD Monitor")
    await channel.send(embed=embed)


# ==========================================================
#  HTTP Handlers
# ==========================================================

async def handle_activity(request: web.Request) -> web.Response:
    """POST /activity — receives signals from vdmonitor.py"""
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    # ── Auth ──────────────────────────────────────────────────────────────
    if not SECRET_TOKEN:
        log.error("VDMONITOR_SECRET_TOKEN is not set in environment/.env")
        return web.Response(status=500, text="Server misconfigured")

    if data.get("token") != SECRET_TOKEN:
        log.warning(f"❌ Invalid token from {request.remote}")
        return web.Response(status=403, text="Forbidden")

    username     = data.get("username", "Unknown").strip()
    status       = data.get("status", "unknown")
    idle_minutes = float(data.get("idle_minutes", 0))
    machine      = data.get("machine", "unknown")

    log.info(f"📨 {username} ({machine}) → {status}, idle={idle_minutes:.1f}m")

    # ── Init state entry ──────────────────────────────────────────────────
    now = datetime.now().astimezone()
    if username not in user_states:
        user_states[username] = {
            "status":     "unknown",
            "last_alert": None,
            "idle_since": None,
            "machine":    machine,
            "last_seen":  now,
        }

    state             = user_states[username]
    state["last_seen"] = now
    state["machine"]   = machine

    # ── Route by status ───────────────────────────────────────────────────

    if status == "idle":
        was_idle       = state["status"] == "idle"
        state["status"] = "idle"

        if not state["idle_since"]:
            state["idle_since"] = now

        should_alert = False
        if idle_minutes >= ALERT_THRESHOLD_MINUTES:
            if not was_idle:
                should_alert = True
            elif state["last_alert"]:
                mins_since = (now - state["last_alert"]).total_seconds() / 60
                if mins_since >= REPEAT_ALERT_MINUTES:
                    should_alert = True

        if should_alert:
            state["last_alert"] = now
            asyncio.create_task(_post_idle_alert(username, idle_minutes))

    elif status == "active":
        prev           = state["status"]
        state["status"] = "active"
        state["idle_since"] = None
        state["last_alert"] = None
        if prev == "idle":
            asyncio.create_task(_post_status_change(username, "active", machine))

    elif status == "heartbeat":
        state["status"] = "active"

    elif status in ("started", "stopped"):
        state["status"] = status
        asyncio.create_task(_post_status_change(username, status, machine))

    return web.Response(status=200, text="OK")


async def handle_status(request: web.Request) -> web.Response:
    """GET /status — returns JSON of all monitored users."""
    result = {
        username: {
            "status":     s["status"],
            "machine":    s["machine"],
            "last_seen":  s["last_seen"].isoformat()  if s["last_seen"]  else None,
            "idle_since": s["idle_since"].isoformat() if s["idle_since"] else None,
        }
        for username, s in user_states.items()
    }
    return web.Response(
        content_type="application/json",
        text=json.dumps(result, indent=2),
    )


# ==========================================================
#  Start
# ==========================================================

async def start_listener():
    """Start the aiohttp server. Called from bot.py."""
    app = web.Application()
    app.router.add_post("/activity", handle_activity)
    app.router.add_get("/status",    handle_status)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, LISTENER_HOST, LISTENER_PORT)
    await site.start()

    log.info(f"✅ VD Monitor Listener → {LISTENER_HOST}:{LISTENER_PORT}")
    return runner