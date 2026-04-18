import os
import json
import csv
import logging
import aiohttp
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
import asyncio

# ==========================================================
#  Setup
# ==========================================================

# Define data directory
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)

# Update file paths
TASK_FILE = os.path.join(DATA_DIR, "tasks.json")
CHECKIN_FILE = os.path.join(DATA_DIR, "checkins.csv")

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")
handler = logging.FileHandler(
    filename=os.path.join(DATA_DIR, "discord.log"), encoding="utf-8", mode="w"
)
logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s"
)

intents = discord.Intents.default()
intents.message_content = True
intents.members = (
    True  # ALSO enable "SERVER MEMBERS INTENT" in Discord Developer Portal
)

bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================================================
#  Persistent Files & Globals
# ==========================================================

# TASK_FILE = "tasks.json"
# CHECKIN_FILE = "checkins.csv"

OLLAMA_URL = "http://192.168.10.7:11434/api/generate"
OLLAMA_MODEL = "llama3"

tasks_db = {}
task_counter = 1
notify_channel_id = None
active_timers = {}

CHECKIN_INTERVAL = 60  # minutes between automatic DMs


# ==========================================================
#  Timezone + End-of-day WhatsApp reminder settings
# ==========================================================
def load_timezone():
    """
    Tries Nicosia first; if not available on this system, falls back to Athens/Famagusta (same time),
    and finally falls back to the machine local timezone.
    """
    tz_keys = ["Asia/Nicosia", "Europe/Athens", "Asia/Famagusta"]
    last_err = None

    for key in tz_keys:
        try:
            tz = ZoneInfo(key)
            print(f"[DEBUG] Using timezone: {key}")
            return tz
        except Exception as e:
            last_err = e

    # Final fallback: local machine timezone
    local_tz = datetime.now().astimezone().tzinfo
    print("[WARNING] Could not load Asia/Nicosia time zone via ZoneInfo.")
    print(
        "[WARNING] Falling back to local machine timezone. Install tzdata to fix this."
    )
    print(f"[WARNING] Last error: {last_err}")
    return local_tz


TIMEZONE = load_timezone()

# Your real end time (15:58 Nicoseia time) is set in the scheduled task below, but you can adjust these constants if needed for testing or different hours.
END_OF_DAY_HOUR = 15
END_OF_DAY_MINUTE = 58

TARGET_ROLE_IDS = [
    1471785941831258114,  # ADMIN
    1471786595236581568,  # IT DEPARTMENT
    1471786692967923744,  # VIDEO EDITING DEPARTMENT
    1471786945632796795,  # WEBSITE DESIGNERS
    1471787838868689091,  # MANAGERS
]

# Create CSV if missing
if not os.path.exists(CHECKIN_FILE):
    with open(CHECKIN_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(["timestamp", "user_id", "username", "reply"])


# ==========================================================
#  Ollama AI Helper
# ==========================================================
async def ask_ollama(prompt: str, system: str = "") -> str:
    """Send a prompt to local Ollama and return the response text."""
    full_prompt = f"{system}\n\n{prompt}" if system else prompt
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OLLAMA_URL,
                json={"model": OLLAMA_MODEL, "prompt": full_prompt, "stream": False},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                data = await resp.json()
                return data.get("response", "").strip()
    except Exception as e:
        print(f"[DEBUG] Ollama error: {e}")
        return None


async def parse_intent(message_text: str, author_name: str) -> dict:
    """
    Ask Ollama to parse a natural language message into a structured intent.
    Returns a dict with 'action' and optional fields.
    """
    task_list = (
        json.dumps(
            {
                k: {
                    "desc": v["desc"],
                    "progress": v["progress"],
                    "status": v["status"],
                    "priority": v["priority"],
                    "assigned": v["assigned"],
                }
                for k, v in tasks_db.items()
            },
            indent=2,
        )
        if tasks_db
        else "No tasks yet."
    )

    system = """You are a task management bot assistant. Parse the user's message and return ONLY a valid JSON object.

Available actions:
- task_add: { "action": "task_add", "description": "..." }
- task_done: { "action": "task_done", "id": <number> }
- task_progress: { "action": "task_progress", "id": <number>, "percent": <0-100> }
- task_status: { "action": "task_status", "id": <number>, "status": "..." }
- task_priority: { "action": "task_priority", "id": <number>, "level": "..." }
- task_info: { "action": "task_info", "id": <number> }
- task_table: { "action": "task_table" }
- rest: { "action": "rest", "minutes": <1-30> }
- question: { "action": "question", "answer": "..." }
- task_assign: { "action": "task_assign", "id": <number>, "assignee": "..." }
- unknown: { "action": "unknown" }

IMPORTANT RULES:
- If the user asks to LIST, SHOW, or VIEW tasks (e.g. "show tasks", "what tasks are pending", "list all tasks"), use task_table.
- If the user asks a QUESTION about tasks (e.g. "how many tasks", "who has tasks", "what is task 2 about"), use question and answer it using the task list.
- Only use task_done, task_info etc. when the user clearly references a specific task ID number.
- Never guess a task ID if the user did not provide one.
- If the user says "assign task X to [name]", use task_assign with the id as a number and assignee as the name string.
- Return ONLY the JSON object, no explanation, no markdown, no extra text."""

    prompt = f"""Current tasks:
{task_list}

User ({author_name}) says: "{message_text}"

Return the JSON intent:"""

    response = await ask_ollama(prompt, system)
    if not response:
        return {"action": "unknown"}

    # Strip any accidental markdown fences
    response = response.strip().strip("```json").strip("```").strip()

    try:
        return json.loads(response)
    except json.JSONDecodeError:
        # Try to extract JSON from response if there's extra text

        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
        return {"action": "unknown"}


async def moderate_message(text: str) -> dict:
    """
    Ask Ollama to check if a message is inappropriate.
    Returns { "flagged": true/false, "reason": "..." }
    """
    system = """You are a content moderator for a professional team Discord server.
Analyze the message and return ONLY a valid JSON object:
{ "flagged": true/false, "reason": "short reason or empty string" }

Flag messages that contain: profanity, insults, harassment, hate speech, or highly unprofessional language.
Do NOT flag: frustration, mild venting, technical jargon, or normal work conversation.
Return ONLY the JSON object."""

    prompt = f'Message: "{text}"'
    response = await ask_ollama(prompt, system)
    if not response:
        # Fallback to simple check
        bad_words = ["shit", "fuck", "ass", "damn", "bitch", "crap"]
        flagged = any(w in text.lower() for w in bad_words)
        return {"flagged": flagged, "reason": "profanity detected" if flagged else ""}

    response = response.strip().strip("```json").strip("```").strip()
    try:
        return json.loads(response)
    except Exception:
        return {"flagged": False, "reason": ""}


async def generate_checkin_reply(username: str, reply_text: str) -> str:
    """Generate a warm, encouraging reply to a check-in response."""
    system = """You are a friendly and supportive team bot for a small company.
A team member just replied to their check-in. Write a short (1-2 sentence), warm, and encouraging response.
Be genuine, not robotic. Vary your responses. Don't use emojis excessively."""

    prompt = f'{username} said: "{reply_text}"'
    response = await ask_ollama(prompt, system)
    return response if response else "✅ Thanks for the update! Keep it up!"


# ==========================================================
#  Task Helpers
# ==========================================================
def progress_bar(percent: int) -> str:
    blocks = max(1, percent // 10)
    return "█" * blocks + "░" * (10 - blocks)


def load_tasks():
    """Load tasks from disk."""
    global tasks_db, task_counter
    try:
        with open(TASK_FILE, "r", encoding="utf-8") as f:
            tasks_db = json.load(f)
        tasks_db = {int(k): v for k, v in tasks_db.items()}
        if tasks_db:
            task_counter = max(tasks_db.keys()) + 1
        print(f"[DEBUG] Loaded {len(tasks_db)} tasks from {TASK_FILE}")
    except FileNotFoundError:
        tasks_db = {}
        print("[DEBUG] No existing tasks file; starting empty.")
    except Exception as e:
        print(f"[DEBUG] Error loading tasks: {e}")
        tasks_db = {}


def save_tasks():
    """Save tasks to disk."""
    try:
        with open(TASK_FILE, "w", encoding="utf-8") as f:
            json.dump(tasks_db, f, ensure_ascii=False, indent=2)
        print(f"[DEBUG] Saved {len(tasks_db)} tasks to {TASK_FILE}")
    except Exception as e:
        print(f"[DEBUG] Error saving tasks: {e}")


async def complete_and_remove(channel, tid, who):
    t = tasks_db.pop(tid, None)
    if t:
        save_tasks()
        await channel.send(
            f"🎉 Great job {who}! Task #{tid} **{t['desc']}** completed 👏"
        )


def format_task_table() -> str:
    if not tasks_db:
        return "No active tasks ✅"
    header = (
        "ID | Prog | Status          | Priority | Assignee | Description\n"
        "---|------|-----------------|----------|-----------|------------"
    )
    rows = [header]
    for i, t in tasks_db.items():
        rows.append(
            f"{i:<2} | {t['progress']:>3}% | {t['status'][:15]:<15} "
            f"| {t['priority'][:8]:<8} | {t['assigned'] or '—':<9} | {t['desc'][:50]}"
        )
    return "```" + "\n".join(rows) + "```"


# ==========================================================
#  Intent Handler — executes parsed AI intents
# ==========================================================
async def handle_intent(message: discord.Message, intent: dict):
    global task_counter
    action = intent.get("action", "unknown")
    channel = message.channel
    author = message.author

    if action == "task_add":
        desc = intent.get("description", "").strip()
        if not desc:
            await channel.send(
                "I couldn't figure out the task description. Could you rephrase?"
            )
            return
        tid = task_counter
        tasks_db[tid] = {
            "desc": desc,
            "progress": 0,
            "status": "Not started",
            "priority": "Normal",
            "assigned": None,
        }
        task_counter += 1
        save_tasks()
        await channel.send(f"🆕 Task #{tid} created: *{desc}*")

    elif action == "task_done":
        tid = int(intent.get("id", 0))
        if tid not in tasks_db:
            await channel.send(f"❌ I couldn't find task #{tid}.")
            return
        await complete_and_remove(
            channel, tid, tasks_db.get(tid, {}).get("assigned") or author.display_name
        )

    elif action == "task_progress":
        tid = int(intent.get("id", 0))
        percent = int(max(0, min(100, intent.get("percent", 0))))
        if tid not in tasks_db:
            await channel.send(f"❌ I couldn't find task #{tid}.")
            return
        t = tasks_db[tid]
        t["progress"] = percent
        save_tasks()
        bar = progress_bar(percent)
        await channel.send(f"📈 Task #{tid}: [{bar}] {percent}%")
        if percent == 100:
            await complete_and_remove(
                channel, tid, t["assigned"] or author.display_name
            )

    elif action == "task_status":
        tid = int(intent.get("id", 0))
        status = intent.get("status", "").strip()
        if tid not in tasks_db:
            await channel.send(f"❌ I couldn't find task #{tid}.")
            return
        tasks_db[tid]["status"] = status
        save_tasks()
        await channel.send(f"ℹ️ Task #{tid} status → **{status}**")

    elif action == "task_priority":
        tid = int(intent.get("id", 0))
        level = intent.get("level", "Normal").capitalize()
        if tid not in tasks_db:
            await channel.send(f"❌ I couldn't find task #{tid}.")
            return
        tasks_db[tid]["priority"] = level
        save_tasks()
        await channel.send(f"🔥 Task #{tid} priority → **{level}**")

    elif action == "task_info":
        tid = int(intent.get("id", 0))
        if tid not in tasks_db:
            await channel.send(f"❌ I couldn't find task #{tid}.")
            return
        t = tasks_db[tid]
        bar = progress_bar(t["progress"])
        await channel.send(
            f"📝 **Task #{tid}**\n"
            f"Desc: {t['desc']}\n"
            f"Progress: [{bar}] {t['progress']}%\n"
            f"Status: {t['status']}\n"
            f"Priority: {t['priority']}\n"
            f"Assigned to: {t['assigned'] or '—'}"
        )

    elif action == "task_table":
        await channel.send(format_task_table())

    elif action == "rest":
        minutes = max(1, min(int(intent.get("minutes", 5)), 30))
        user = author.id
        if user in active_timers:
            await channel.send(
                f"⏳ {author.mention}, you already have a timer running!"
            )
            return
        await channel.send(
            f"🕓 Starting a {minutes}-minute break for {author.mention} ☕"
        )
        active_timers[user] = True

        async def run_timer():
            await asyncio.sleep(minutes * 60)
            if user in active_timers:
                await channel.send(f"🔔 Break's over {author.mention}! Back to work!")
            active_timers.pop(user, None)

        asyncio.create_task(run_timer())

    elif action == "question":
        answer = intent.get("answer", "").strip()
        if answer:
            await channel.send(answer)
        else:
            await channel.send(
                "I'm not sure about that one. Could you be more specific?"
            )

    elif action == "task_assign":
        tid = int(intent.get("id", 0))
        assignee = intent.get("assignee", "").strip()
        if tid not in tasks_db:
            await channel.send(f"❌ I couldn't find task #{tid}.")
            return
        tasks_db[tid]["assigned"] = assignee
        save_tasks()
        await channel.send(f"👥 Task #{tid} assigned to **{assignee}**")

    else:
        await channel.send(
            "I'm not sure what you mean. You can talk to me naturally — try something like:\n"
            '• *"add a task to review the website"*\n'
            '• *"mark task 3 as done"*\n'
            '• *"what tasks are pending?"*\n'
            '• *"I need a 10 minute break"*'
        )


# ==========================================================
#  Events
# ==========================================================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} — ID: {bot.user.id}")
    load_tasks()

    # on_ready can fire multiple times (reconnect), so guard with is_running()
    if not workday_reminder.is_running():
        workday_reminder.start()

    if not user_checkin.is_running():
        user_checkin.start()

    if not end_of_day_whatsapp_reminder.is_running():
        end_of_day_whatsapp_reminder.start()


@bot.event
async def on_disconnect():
    save_tasks()


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # --- DM: check-in reply handling
    if isinstance(message.channel, discord.DMChannel):
        reply_text = message.content.strip()

        # Log to CSV
        row = [
            datetime.now().isoformat(timespec="seconds"),
            message.author.id,
            message.author.display_name,
            reply_text,
        ]
        with open(CHECKIN_FILE, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)

        # AI-generated reply
        ai_reply = await generate_checkin_reply(message.author.display_name, reply_text)
        await message.channel.send(ai_reply)

        # Forward reply to server channel
        if bot.guilds:
            guild = bot.guilds[0]
            chan = discord.utils.get(guild.text_channels, name="check-in-responses")
            if not chan:
                chan = discord.utils.get(guild.text_channels, name="general")
            if chan:
                embed = discord.Embed(
                    title=f"💬 Check‑in from {message.author.display_name}",
                    description=reply_text,
                    timestamp=datetime.now(),
                    color=0x4CAF50,
                )
                await chan.send(embed=embed)
        return

    # --- AI profanity/tone moderation
    moderation = await moderate_message(message.content)
    if moderation.get("flagged"):
        try:
            await message.delete()
            reason = moderation.get("reason", "inappropriate language")
            await message.channel.send(
                f"{message.author.mention}, please keep it professional. ({reason})"
            )
        except discord.Forbidden:
            pass
        return

    # --- Still support legacy ! commands
    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    # --- Natural language: parse intent with AI
    # Only respond if bot is mentioned OR message is in a DM-like focused channel
    bot_mentioned = bot.user in message.mentions
    is_reply_to_bot = (
        message.reference
        and message.reference.resolved
        and getattr(message.reference.resolved, "author", None) == bot.user
    )

    if bot_mentioned or is_reply_to_bot:
        # Strip the mention from the message
        text = message.content.replace(f"<@{bot.user.id}>", "").strip()
        if not text:
            await message.channel.send(
                "Hey! How can I help? Try asking me to add a task, check progress, or take a break."
            )
            return

        async with message.channel.typing():
            intent = await parse_intent(text, message.author.display_name)
            await handle_intent(message, intent)


# ==========================================================
#  Legacy ! Commands (still work as backup)
# ==========================================================
@bot.command()
async def task_add(ctx, *, description: str):
    """Create a new task."""
    global task_counter
    tid = task_counter
    tasks_db[tid] = {
        "desc": description,
        "progress": 0,
        "status": "Not started",
        "priority": "Normal",
        "assigned": None,
    }
    task_counter += 1
    save_tasks()
    await ctx.send(f"🆕 Task #{tid} created: *{description}*")


@bot.command()
async def task_assign(ctx, tid: int, member: discord.Member):
    """Assign a task to a user."""
    if tid not in tasks_db:
        await ctx.send("❌ Task not found.")
        return
    tasks_db[tid]["assigned"] = member.display_name
    save_tasks()
    await ctx.send(f"👥 Task #{tid} assigned to **{member.display_name}**")


@bot.command()
async def task_progress(ctx, tid: int, percent: int):
    if tid not in tasks_db:
        await ctx.send("❌ Task not found.")
        return
    percent = int(max(0, min(100, percent)))
    t = tasks_db[tid]
    t["progress"] = percent
    save_tasks()
    bar = progress_bar(percent)
    await ctx.send(f"📈 Task #{tid}: [{bar}] {percent}%")
    if percent == 100:
        await complete_and_remove(
            ctx.channel, tid, t["assigned"] or ctx.author.display_name
        )


@bot.command()
async def task_done(ctx, tid: int):
    """Mark task as done."""
    if tid not in tasks_db:
        await ctx.send("❌ Task not found.")
        return
    await complete_and_remove(
        ctx.channel, tid, tasks_db[tid]["assigned"] or ctx.author.display_name
    )


@bot.command()
async def task_status(ctx, tid: int, *, new_status: str):
    if tid not in tasks_db:
        await ctx.send("❌ Task not found.")
        return
    tasks_db[tid]["status"] = new_status
    save_tasks()
    await ctx.send(f"ℹ️ Task #{tid} status → **{new_status}**")


@bot.command()
async def task_priority(ctx, tid: int, *, level: str):
    if tid not in tasks_db:
        await ctx.send("❌ Task not found.")
        return
    tasks_db[tid]["priority"] = level.capitalize()
    save_tasks()
    await ctx.send(f"🔥 Task #{tid} priority → **{level.capitalize()}**")


@bot.command()
async def task_info(ctx, tid: int):
    """Show full details for one task."""
    if tid not in tasks_db:
        await ctx.send("❌ Task not found.")
        return
    t = tasks_db[tid]
    bar = progress_bar(t["progress"])
    await ctx.send(
        f"📝 **Task #{tid}**\n"
        f"Desc: {t['desc']}\n"
        f"Progress: [{bar}] {t['progress']}%\n"
        f"Status: {t['status']}\n"
        f"Priority: {t['priority']}\n"
        f"Assigned to: {t['assigned'] or '—'}"
    )


# Show all active tasks.
@bot.command()
async def task_table(ctx):
    await ctx.send(format_task_table())


# ==========================================================
#  ☕ Rest Break Command
# ==========================================================
@bot.command()
async def rest(ctx, minutes: int = 5):
    """Start a timed break (default 5 min)."""
    user = ctx.author.id
    if user in active_timers:
        await ctx.send(f"⏳ {ctx.author.mention}, you already have a timer running!")
        return
    minutes = max(1, min(minutes, 30))
    await ctx.send(f"🕓 Starting a {minutes}-minute break for {ctx.author.mention} ☕")
    active_timers[user] = True

    async def run_timer():
        await asyncio.sleep(minutes * 60)
        if user in active_timers:
            await ctx.send(f"🔔 Break's over {ctx.author.mention}! Back to work!")
        active_timers.pop(user, None)

    asyncio.create_task(run_timer())


@bot.command(name="time")
async def time_cmd(ctx):
    lj = datetime.now(TIMEZONE)
    utc = discord.utils.utcnow()
    await ctx.send(
        "```"
        f"Bot TZ now: {lj.isoformat(timespec='seconds')}\n"
        f"UTC:       {utc.isoformat(timespec='seconds')}\n"
        "```"
    )


# ==========================================================
#  Work‑Hour Reminders
# ==========================================================
@bot.command()
async def setnotify(ctx):
    """Choose this channel for hourly work reminders."""
    global notify_channel_id
    notify_channel_id = ctx.channel.id
    await ctx.send("📅 Work‑hour reminders will appear here!")


# ==========================================================
#  EOD Test Command
# ==========================================================
@bot.command()
@commands.has_permissions(administrator=True)
async def test_eod(ctx):
    """Manually trigger the end-of-day reminder for testing."""
    await ctx.send("⏰ Triggering EOD reminder now...")
    # await end_of_day_whatsapp_reminder()
    await end_of_day_whatsapp_reminder.coro(end_of_day_whatsapp_reminder)
    await ctx.send("✅ Done. Check DMs and logs.")


# ==========================================================
#  Scheduled Tasks
# ==========================================================
@tasks.loop(minutes=60)
async def workday_reminder():
    if not notify_channel_id:
        return
    now = datetime.now(TIMEZONE)
    if now.weekday() < 5 and 9 <= now.hour <= 17:
        channel = bot.get_channel(notify_channel_id)
        if channel:
            await channel.send(
                "☀️ Good day team! Check `!task_table` and stay focused 💪"
            )


@workday_reminder.before_loop
async def before_workday_reminder():
    await bot.wait_until_ready()
    print("[DEBUG] Workday reminder loop started.")


# ==========================================================
#  🌿 Automatic User Check‑Ins
# ==========================================================
@tasks.loop(minutes=CHECKIN_INTERVAL)
async def user_checkin():
    """DM each active member a quick check‑in question."""
    print("[DEBUG] Running user_checkin loop…")
    for guild in bot.guilds:
        try:
            await guild.chunk()
        except Exception:
            pass
        for member in guild.members:
            if member.bot or member.status == discord.Status.offline:
                continue
            try:
                await member.send(
                    f"🌿 Hey {member.display_name}! "
                    "How are you doing right now, and what are you working on?"
                )
                await asyncio.sleep(2)
            except discord.Forbidden:
                # fallback to general channel
                channel = discord.utils.get(guild.text_channels, name="general")
                if channel:
                    await channel.send(
                        f"{member.mention} 🌿 How are you doing right now?"
                    )
                await asyncio.sleep(2)


@user_checkin.before_loop
async def before_user_checkin():
    await bot.wait_until_ready()


# ==========================================================
#  ✅ End-of-day WhatsApp Reminder (15:58 Nicosia time, Mon–Fri)
# ==========================================================
@tasks.loop(minutes=1)
async def end_of_day_whatsapp_reminder():
    now = datetime.now(TIMEZONE)
    if now.weekday() >= 5:  # Skip weekends
        return
    if not (now.hour == END_OF_DAY_HOUR and now.minute == END_OF_DAY_MINUTE):
        return  # Not time yet

    print(f"[DEBUG] end_of_day_whatsapp_reminder firing at {now.isoformat()}")
    text = (
        "Hello!\n"
        "The workday has ended — please send a short summary in WhatsApp of what you worked on today "
        "(key tasks + current status). Thank you!"
    )
    for guild in bot.guilds:
        try:
            await guild.chunk()
        except Exception:
            pass

        # Collect unique members across target roles (avoid duplicates)
        targets = {}
        for rid in TARGET_ROLE_IDS:
            role = guild.get_role(rid)
            if not role:
                print(f"[DEBUG] Role {rid} not found in guild {guild.name}")
                continue
            for m in role.members:
                if not m.bot:
                    targets[m.id] = m
        print(
            f"[DEBUG] end_of_day_whatsapp_reminder targets={len(targets)} guild={guild.name}"
        )
        for member in targets.values():
            try:
                await member.send(text)
                print(f"[DEBUG] Sent EOD DM to {member.display_name}")
                await asyncio.sleep(0.8)
            except discord.Forbidden:
                print(f"[DEBUG] Forbidden: {member.display_name} has DMs closed")
            except Exception as e:
                print(f"[DEBUG] Failed to DM {member.display_name}: {e}")


@end_of_day_whatsapp_reminder.before_loop
async def before_end_of_day_whatsapp_reminder():
    await bot.wait_until_ready()
    print("[DEBUG] end_of_day_whatsapp_reminder loop started.")


# ==========================================================
#  Run
# ==========================================================
bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)
