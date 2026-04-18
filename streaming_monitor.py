import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

# ==========================================================
#  Config
# ==========================================================

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
STREAMING_FILE = os.path.join(DATA_DIR, "streaming.json")
LOG_CHANNEL_NAME = os.getenv("STREAMING_LOG_CHANNEL", "streaming-logs")

# Will be set by setup() from the main bot's TIMEZONE
TIMEZONE = None

# { "user_id_str": { "username": "...", "sessions": [...], "current_start": "..." } }
streaming_db = {}


# ==========================================================
#  Load / Save
# ==========================================================

def load_streaming():
    global streaming_db
    try:
        with open(STREAMING_FILE, "r", encoding="utf-8") as f:
            streaming_db = json.load(f)
        print(f"[STREAM] Loaded streaming data for {len(streaming_db)} users")
    except FileNotFoundError:
        streaming_db = {}
    except Exception as e:
        print(f"[STREAM] Error loading streaming data: {e}")
        streaming_db = {}


def save_streaming():
    try:
        with open(STREAMING_FILE, "w", encoding="utf-8") as f:
            json.dump(streaming_db, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[STREAM] Error saving streaming data: {e}")


# ==========================================================
#  Helpers
# ==========================================================

def _get_entry(member: discord.Member) -> dict:
    """Get or create a streaming entry for a member."""
    user_id = str(member.id)
    if user_id not in streaming_db:
        streaming_db[user_id] = {
            "username": member.display_name,
            "user_id": member.id,
            "sessions": [],
            "current_start": None,
            "voice_sessions": [],
            "current_voice_start": None,
        }
    # Add voice fields if missing (for older entries)
    entry = streaming_db[user_id]
    entry.setdefault("voice_sessions", [])
    entry.setdefault("current_voice_start", None)
    entry["username"] = member.display_name
    return entry


def _format_duration(minutes: float) -> str:
    """Format minutes into a readable string."""
    if minutes < 60:
        return f"{round(minutes, 1)} min"
    hours = minutes / 60
    return f"{round(hours, 2)} hrs ({round(minutes, 1)} min)"


def _get_log_channel(bot) -> discord.TextChannel | None:
    """Find the streaming log channel across all guilds."""
    for guild in bot.guilds:
        chan = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
        if chan:
            return chan
    return None


# ==========================================================
#  Core Event Handler
# ==========================================================

async def handle_voice_state_update(
    bot,
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    """Called from the main bot's on_voice_state_update event."""
    if member.bot:
        return

    now = datetime.now(TIMEZONE)
    now_iso = now.isoformat(timespec="seconds")
    entry = _get_entry(member)
    log_chan = _get_log_channel(bot)

    was_in_voice = before.channel is not None
    is_in_voice = after.channel is not None
    was_streaming = before.self_stream or False
    is_streaming = after.self_stream or False

    joined_voice = not was_in_voice and is_in_voice
    left_voice = was_in_voice and not is_in_voice

    # ----------------------------------------------------------
    # Key fix: if user leaves voice while self_stream is still True,
    # Discord does NOT set self_stream to False — it stays True.
    # So we must treat "left voice while streaming" as stream_stopped.
    # ----------------------------------------------------------
    stream_started = not was_streaming and is_streaming and not left_voice
    stream_stopped = (
        (was_streaming and not is_streaming)            # Normal: stopped Go Live
        or (was_streaming and left_voice)               # Left voice while still streaming
        or (entry.get("current_start") and left_voice)  # Had active stream + left voice
    )

    # Nothing relevant changed
    if not any([stream_started, stream_stopped, joined_voice, left_voice]):
        return

    # ----------------------------------------------------------
    # Calculate durations before updating state
    # ----------------------------------------------------------
    stream_duration_minutes = None
    stream_start_time = None
    voice_duration_minutes = None
    voice_start_time = None

    if stream_stopped:
        start_iso = entry.get("current_start")
        if start_iso:
            stream_start_time = datetime.fromisoformat(start_iso)
            stream_duration_seconds = (now - stream_start_time).total_seconds()
            stream_duration_minutes = round(stream_duration_seconds / 60, 1)

            session = {
                "date": now.strftime("%Y-%m-%d"),
                "start": start_iso,
                "end": now_iso,
                "start_readable": stream_start_time.strftime("%Y-%m-%d %H:%M:%S"),
                "end_readable": now.strftime("%Y-%m-%d %H:%M:%S"),
                "duration_minutes": stream_duration_minutes,
            }
            entry["sessions"].append(session)
            entry["current_start"] = None

            print(
                f"[STREAM] ⚫ {member.display_name} (ID: {member.id}) "
                f"stopped streaming at {now.strftime('%Y-%m-%d %H:%M:%S')} — "
                f"Duration: {_format_duration(stream_duration_minutes)}"
            )
        else:
            print(
                f"[STREAM] ⚠️ {member.display_name} stopped streaming but no start time recorded"
            )

    if left_voice:
        voice_start_iso = entry.get("current_voice_start")
        if voice_start_iso:
            voice_start_time = datetime.fromisoformat(voice_start_iso)
            voice_duration_seconds = (now - voice_start_time).total_seconds()
            voice_duration_minutes = round(voice_duration_seconds / 60, 1)

            voice_session = {
                "date": now.strftime("%Y-%m-%d"),
                "start": voice_start_iso,
                "end": now_iso,
                "start_readable": voice_start_time.strftime("%Y-%m-%d %H:%M:%S"),
                "end_readable": now.strftime("%Y-%m-%d %H:%M:%S"),
                "duration_minutes": voice_duration_minutes,
                "channel": before.channel.name if before.channel else "Unknown",
            }
            entry["voice_sessions"].append(voice_session)
            entry["current_voice_start"] = None

            print(
                f"[VOICE] 🔴 {member.display_name} (ID: {member.id}) "
                f"left #{before.channel.name} at {now.strftime('%Y-%m-%d %H:%M:%S')} — "
                f"Duration: {_format_duration(voice_duration_minutes)}"
            )

    if joined_voice:
        entry["current_voice_start"] = now_iso
        print(
            f"[VOICE] 🟢 {member.display_name} (ID: {member.id}) "
            f"joined #{after.channel.name} at {now.strftime('%Y-%m-%d %H:%M:%S')}"
        )

    if stream_started:
        entry["current_start"] = now_iso
        print(
            f"[STREAM] 🔴 {member.display_name} (ID: {member.id}) "
            f"started streaming in #{after.channel.name} at {now.strftime('%Y-%m-%d %H:%M:%S')}"
        )

    save_streaming()

    # ----------------------------------------------------------
    # Build a single combined notification
    # ----------------------------------------------------------
    if not log_chan:
        return

    today = now.strftime("%Y-%m-%d")

    # ---- Joined voice ----
    if joined_voice and not stream_started and not stream_stopped and not left_voice:
        embed = discord.Embed(
            title=f"🟢 {member.display_name} joined a voice channel",
            color=0x2ECC71,
            timestamp=now,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(
            name="👤 User",
            value=f"{member.mention} (`{member.id}`)",
            inline=False,
        )
        embed.add_field(
            name="🔊 Channel",
            value=after.channel.name,
            inline=True,
        )
        embed.add_field(
            name="🕐 Joined at",
            value=now.strftime("%Y-%m-%d %H:%M:%S"),
            inline=True,
        )
        await log_chan.send(embed=embed)
        return

    # ---- Started streaming only (already in voice) ----
    if stream_started and not joined_voice and not left_voice:
        embed = discord.Embed(
            title=f"🔴 {member.display_name} started streaming",
            color=0xFF0000,
            timestamp=now,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(
            name="👤 User",
            value=f"{member.mention} (`{member.id}`)",
            inline=False,
        )
        embed.add_field(
            name="🔊 Voice Channel",
            value=after.channel.name if after.channel else "Unknown",
            inline=True,
        )
        embed.add_field(
            name="🕐 Started at",
            value=now.strftime("%Y-%m-%d %H:%M:%S"),
            inline=True,
        )
        # Show how long they have already been in voice
        voice_start_iso = entry.get("current_voice_start")
        if voice_start_iso:
            voice_start_dt = datetime.fromisoformat(voice_start_iso)
            already_in_voice = (now - voice_start_dt).total_seconds() / 60
            embed.add_field(
                name="🔊 Already in voice for",
                value=_format_duration(round(already_in_voice, 1)),
                inline=True,
            )
        await log_chan.send(embed=embed)
        return

    # ---- Stopped streaming only (still in voice) ----
    if stream_stopped and not left_voice:
        today_stream_total = sum(
            s["duration_minutes"] for s in entry["sessions"] if s["date"] == today
        )
        embed = discord.Embed(
            title=f"⚫ {member.display_name} stopped streaming",
            color=0x808080,
            timestamp=now,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(
            name="👤 User",
            value=f"{member.mention} (`{member.id}`)",
            inline=False,
        )
        if stream_start_time and stream_duration_minutes is not None:
            embed.add_field(
                name="🕐 Stream session",
                value=f"{stream_start_time.strftime('%H:%M:%S')} → {now.strftime('%H:%M:%S')}",
                inline=True,
            )
            embed.add_field(
                name="⏱️ Stream duration",
                value=_format_duration(stream_duration_minutes),
                inline=True,
            )
        embed.add_field(
            name="📅 Today's stream total",
            value=f"{len([s for s in entry['sessions'] if s['date'] == today])} sessions — "
                  f"{_format_duration(today_stream_total)}",
            inline=False,
        )
        # Show how long they are still in voice
        voice_start_iso = entry.get("current_voice_start")
        if voice_start_iso:
            voice_start_dt = datetime.fromisoformat(voice_start_iso)
            still_in_voice = (now - voice_start_dt).total_seconds() / 60
            embed.add_field(
                name="🔊 Still in voice for",
                value=_format_duration(round(still_in_voice, 1)),
                inline=True,
            )
        await log_chan.send(embed=embed)
        return

    # ---- Left voice (with or without streaming) ----
    # This handles both: left voice only AND left voice while streaming
    if left_voice:
        today_voice_total = sum(
            s["duration_minutes"]
            for s in entry.get("voice_sessions", [])
            if s["date"] == today
        )
        today_stream_total = sum(
            s["duration_minutes"] for s in entry["sessions"] if s["date"] == today
        )
        today_stream_sessions = [s for s in entry["sessions"] if s["date"] == today]

        # Title changes depending on whether they were streaming
        if stream_stopped and stream_duration_minutes is not None:
            title = f"📴 {member.display_name} left voice while streaming"
            color = 0xE74C3C
        else:
            title = f"🔇 {member.display_name} left voice channel"
            color = 0x3498DB

        embed = discord.Embed(
            title=title,
            color=color,
            timestamp=now,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(
            name="👤 User",
            value=f"{member.mention} (`{member.id}`)",
            inline=False,
        )
        embed.add_field(
            name="🔊 Channel",
            value=before.channel.name if before.channel else "Unknown",
            inline=False,
        )

        # ---- Voice session stats ----
        embed.add_field(name="\u200b", value="**🔊 Voice Session:**", inline=False)
        if voice_start_time and voice_duration_minutes is not None:
            embed.add_field(
                name="🕐 Voice session",
                value=f"{voice_start_time.strftime('%H:%M:%S')} → {now.strftime('%H:%M:%S')}",
                inline=True,
            )
            embed.add_field(
                name="⏱️ Voice duration",
                value=_format_duration(voice_duration_minutes),
                inline=True,
            )
        embed.add_field(
            name="📅 Today's voice total",
            value=f"{len([s for s in entry.get('voice_sessions', []) if s['date'] == today])} sessions — "
                  f"{_format_duration(today_voice_total)}",
            inline=False,
        )

        # ---- Stream session stats ----
        embed.add_field(name="\u200b", value="**🔴 Stream Today:**", inline=False)
        if stream_stopped and stream_start_time and stream_duration_minutes is not None:
            embed.add_field(
                name="🕐 Last stream session",
                value=f"{stream_start_time.strftime('%H:%M:%S')} → {now.strftime('%H:%M:%S')}",
                inline=True,
            )
            embed.add_field(
                name="⏱️ Stream duration",
                value=_format_duration(stream_duration_minutes),
                inline=True,
            )
        if today_stream_sessions:
            embed.add_field(
                name="📅 Today's stream total",
                value=f"{len(today_stream_sessions)} sessions — {_format_duration(today_stream_total)}",
                inline=False,
            )
            # for i, s in enumerate(today_stream_sessions, 1):
            #     start_t = s.get("start_readable", s["start"])
            #     end_t = s.get("end_readable", s["end"])
            #     embed.add_field(
            #         name=f"  Stream session {i}",
            #         value=f"`{start_t}` → `{end_t}` — {_format_duration(s['duration_minutes'])}",
            #         inline=False,
            #     )
        else:
            embed.add_field(
                name="📅 Today's stream total",
                value="No streaming today",
                inline=False,
            )

        await log_chan.send(embed=embed)
        return


# ==========================================================
#  Commands (registered on the bot by setup())
# ==========================================================

async def cmd_streaming(ctx, member: discord.Member = None):
    """Check streaming and voice stats for a user."""
    member = member or ctx.author
    user_id = str(member.id)
    entry = streaming_db.get(user_id)

    if not entry or (not entry["sessions"] and not entry.get("voice_sessions")):
        await ctx.send(f"📊 No streaming or voice data found for **{member.display_name}**.")
        return

    now = datetime.now(TIMEZONE)
    today = now.strftime("%Y-%m-%d")

    # ---- Stream sessions today ----
    today_sessions = [s for s in entry["sessions"] if s["date"] == today]
    today_total = sum(s["duration_minutes"] for s in today_sessions)

    # ---- Voice sessions today ----
    today_voice_sessions = [s for s in entry.get("voice_sessions", []) if s["date"] == today]
    today_voice_total = sum(s["duration_minutes"] for s in today_voice_sessions)

    embed = discord.Embed(
        title=f"📊 Streaming & Voice Stats — {member.display_name}",
        color=0x9146FF,
        timestamp=now,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="👤 User ID", value=f"`{member.id}`", inline=False)

    # ---- Currently streaming? ----
    if entry.get("current_start"):
        start_time = datetime.fromisoformat(entry["current_start"])
        live_minutes = (now - start_time).total_seconds() / 60
        embed.add_field(
            name="🔴 LIVE NOW",
            value=f"Since {start_time.strftime('%H:%M:%S')} ({_format_duration(round(live_minutes, 1))} so far)",
            inline=False,
        )

    # ---- Currently in voice? ----
    if entry.get("current_voice_start"):
        voice_start = datetime.fromisoformat(entry["current_voice_start"])
        live_voice = (now - voice_start).total_seconds() / 60
        embed.add_field(
            name="🔊 CURRENTLY IN VOICE",
            value=f"Since {voice_start.strftime('%H:%M:%S')} ({_format_duration(round(live_voice, 1))} so far)",
            inline=False,
        )

    # ---- Stream sessions today ----
    embed.add_field(
        name=f"🔴 Stream today ({today})",
        value=(
            f"{len(today_sessions)} sessions — {_format_duration(today_total)}"
            if today_sessions
            else "No streaming today"
        ),
        inline=False,
    )
    # for i, s in enumerate(today_sessions, 1):
    #     start_t = s.get("start_readable", s["start"])
    #     end_t = s.get("end_readable", s["end"])
    #     embed.add_field(
    #         name=f"  Stream session {i}",
    #         value=f"`{start_t}` → `{end_t}`\n⏱️ {_format_duration(s['duration_minutes'])}",
    #         inline=True,
    #     )

    # ---- Voice sessions today ----
    embed.add_field(
        name=f"🔊 Voice today ({today})",
        value=(
            f"{len(today_voice_sessions)} sessions — {_format_duration(today_voice_total)}"
            if today_voice_sessions
            else "No voice activity today"
        ),
        inline=False,
    )
    for i, s in enumerate(today_voice_sessions, 1):
        start_t = s.get("start_readable", s["start"])
        end_t = s.get("end_readable", s["end"])
        embed.add_field(
            name=f"  Voice session {i}",
            value=f"`{start_t}` → `{end_t}`\n⏱️ {_format_duration(s['duration_minutes'])} in #{s.get('channel', '?')}",
            inline=True,
        )

    # ---- Recent days summary ----
    all_stream_dates = set(s["date"] for s in entry["sessions"])
    all_voice_dates = set(s["date"] for s in entry.get("voice_sessions", []))
    all_dates = sorted(all_stream_dates | all_voice_dates)
    recent_dates = [d for d in all_dates if d != today][-6:]

    if recent_dates:
        embed.add_field(name="\u200b", value="**📆 Recent days:**", inline=False)
        for date in reversed(recent_dates):
            day_stream_sessions = [s for s in entry["sessions"] if s["date"] == date]
            day_stream_total = sum(s["duration_minutes"] for s in day_stream_sessions)
            day_voice_sessions = [s for s in entry.get("voice_sessions", []) if s["date"] == date]
            day_voice_total = sum(s["duration_minutes"] for s in day_voice_sessions)
            embed.add_field(
                name=date,
                value=(
                    f"🔴 {len(day_stream_sessions)} stream sessions — {_format_duration(day_stream_total)}\n"
                    f"🔊 {len(day_voice_sessions)} voice sessions — {_format_duration(day_voice_total)}"
                ),
                inline=True,
            )

    # ---- All-time totals ----
    all_stream_total = sum(s["duration_minutes"] for s in entry["sessions"])
    all_voice_total = sum(s["duration_minutes"] for s in entry.get("voice_sessions", []))
    embed.set_footer(
        text=(
            f"All-time — "
            f"🔴 {len(entry['sessions'])} stream sessions ({_format_duration(all_stream_total)}) | "
            f"🔊 {len(entry.get('voice_sessions', []))} voice sessions ({_format_duration(all_voice_total)})"
        )
    )

    await ctx.send(embed=embed)


async def cmd_streaming_today(ctx):
    """Show everyone's streaming and voice activity today."""
    now = datetime.now(TIMEZONE)
    today = now.strftime("%Y-%m-%d")

    lines = []
    for user_id, entry in streaming_db.items():
        today_sessions = [s for s in entry["sessions"] if s["date"] == today]
        today_voice_sessions = [
            s for s in entry.get("voice_sessions", []) if s["date"] == today
        ]

        # Skip users with no activity today at all
        if (
            not today_sessions
            and not today_voice_sessions
            and not entry.get("current_start")
            and not entry.get("current_voice_start")
        ):
            continue

        today_total = sum(s["duration_minutes"] for s in today_sessions)
        today_voice_total = sum(s["duration_minutes"] for s in today_voice_sessions)
        name = entry.get("username", f"User {user_id}")
        live_tag = " 🔴 LIVE" if entry.get("current_start") else ""
        voice_tag = " 🔊 IN VOICE" if entry.get("current_voice_start") else ""

        lines.append(
            f"**{name}** (`{user_id}`){live_tag}{voice_tag}\n"
            f"  🔴 Stream: {len(today_sessions)} sessions — {_format_duration(today_total)}\n"
            f"  🔊 Voice: {len(today_voice_sessions)} sessions — {_format_duration(today_voice_total)}"
        )

    if not lines:
        await ctx.send(f"📊 No voice or streaming activity recorded today ({today}).")
        return

    embed = discord.Embed(
        title=f"📊 Streaming & Voice Activity — {today}",
        description="\n\n".join(lines),
        color=0x9146FF,
        timestamp=now,
    )
    embed.set_footer(text=f"Total active users: {len(lines)}")
    await ctx.send(embed=embed)


# ==========================================================
#  Setup — call this from your main bot file
# ==========================================================

def setup(bot, timezone):
    """
    Call this once from your main file to register everything.

    Usage in main file:
        import streaming_monitor
        streaming_monitor.setup(bot, TIMEZONE)
    """
    global TIMEZONE
    TIMEZONE = timezone

    load_streaming()

    # Register the voice state event (NOT presence)
    @bot.event
    async def on_voice_state_update(member, before, after):
        await handle_voice_state_update(bot, member, before, after)

    # Register commands
    @bot.command(name="streaming")
    async def _streaming(ctx, member: discord.Member = None):
        """Check streaming stats. Usage: !streaming @user"""
        await cmd_streaming(ctx, member)

    @bot.command(name="streaming_today")
    async def _streaming_today(ctx):
        """Show everyone who streamed today."""
        await cmd_streaming_today(ctx)

    print("[STREAM] ✅ Streaming monitor loaded.")