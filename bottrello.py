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

handler = logging.FileHandler(
    filename=os.path.join(DATA_DIR, "discord.log"), encoding="utf-8", mode="w"
)
logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s"
)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # Also enable "SERVER MEMBERS INTENT" in the Discord Developer Portal

bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================================================
#  Persistent Files & Globals
# ==========================================================

OLLAMA_URL = "http://192.168.10.7:11434/api/generate"
OLLAMA_MODEL = "llama3"

tasks_db = {}       # { task_id: task_dict } — loaded from disk on startup
task_counter = 1    # auto-increments with each new task
notify_channel_id = None  # set via !setnotify; used for hourly work reminders
active_timers = {}  # { user_id: True } — tracks in-progress break timers

CHECKIN_INTERVAL = 60  # minutes between automatic check-in DMs

# Create CSV if missing
if not os.path.exists(CHECKIN_FILE):
    with open(CHECKIN_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(["timestamp", "user_id", "username", "reply"])


# ==========================================================
#  Timezone + End-of-day WhatsApp reminder settings
# ==========================================================

def load_timezone():
    """
    Tries Nicosia first, falls back to Athens/Famagusta (same UTC offset),
    and finally falls back to the machine's local timezone.
    Install the tzdata package if ZoneInfo keys are missing.
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
    print("[WARNING] Could not load Nicosia timezone. Falling back to local machine timezone.")
    print("[WARNING] Install the tzdata package to fix this.")
    print(f"[WARNING] Last error: {last_err}")
    return local_tz


TIMEZONE = load_timezone()

# End-of-day reminder time (15:58 Nicosia local time). Adjust as needed.
END_OF_DAY_HOUR = 15
END_OF_DAY_MINUTE = 58

# Role IDs that receive the end-of-day WhatsApp summary reminder
TARGET_ROLE_IDS = [
    1471785941831258114,  # ADMIN
    1471786595236581568,  # IT DEPARTMENT
    1471786692967923744,  # VIDEO EDITING DEPARTMENT
    1471786945632796795,  # WEBSITE DESIGNERS
    1471787838868689091,  # MANAGERS
    1490641426005102632,  # ADMIN (test server)
]


# ==========================================================
#  🟦 Trello Integration
# ==========================================================

TRELLO_API_KEY = os.getenv("TRELLO_API_KEY", "")
TRELLO_TOKEN = os.getenv("TRELLO_TOKEN", "")
TRELLO_BOARD_ID = os.getenv("TRELLO_BOARD_ID", "")
TRELLO_BASE = "https://api.trello.com/1"

# Populated on startup by _trello_init()
_trello_list_ids = {
    "todo": None,
    "in_progress": None,
    "done": None,
}

# Maps internal task ID → Trello card ID; persisted to disk so mappings survive restarts
TRELLO_MAP_FILE = os.path.join(DATA_DIR, "trello_map.json")
trello_map: dict[int, str] = {}  # { task_id: trello_card_id }


def _trello_enabled() -> bool:
    return bool(TRELLO_API_KEY and TRELLO_TOKEN and TRELLO_BOARD_ID)


def _trello_params(**extra) -> dict:
    """Build base query params (key + token) for every Trello API request."""
    return {"key": TRELLO_API_KEY, "token": TRELLO_TOKEN, **extra}


def load_trello_map():
    global trello_map
    try:
        with open(TRELLO_MAP_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        trello_map = {int(k): v for k, v in raw.items()}
        print(f"[TRELLO] Loaded {len(trello_map)} card mappings.")
    except FileNotFoundError:
        trello_map = {}
    except Exception as e:
        print(f"[TRELLO] Error loading trello_map: {e}")
        trello_map = {}


def save_trello_map():
    try:
        with open(TRELLO_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(trello_map, f, indent=2)
    except Exception as e:
        print(f"[TRELLO] Error saving trello_map: {e}")


async def _trello_init():
    """
    On startup, discover existing list IDs on the board by name.
    Recognized names (case-insensitive):
      - To Do:       "to do", "todo", "backlog"
      - In Progress: "in progress", "doing", "wip"
      - Done:        "done", "completed", "finished"
    Creates any missing lists automatically.
    """
    if not _trello_enabled():
        print("[TRELLO] Env vars not set — Trello sync disabled.")
        return

    url = f"{TRELLO_BASE}/boards/{TRELLO_BOARD_ID}/lists"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=_trello_params()) as resp:
                lists = await resp.json()

        todo_names = {"to do", "todo", "backlog"}
        in_progress_names = {"in progress", "doing", "wip"}
        done_names = {"done", "completed", "finished"}

        for lst in lists:
            name_lower = lst["name"].lower()
            if name_lower in todo_names and not _trello_list_ids["todo"]:
                _trello_list_ids["todo"] = lst["id"]
            elif name_lower in in_progress_names and not _trello_list_ids["in_progress"]:
                _trello_list_ids["in_progress"] = lst["id"]
            elif name_lower in done_names and not _trello_list_ids["done"]:
                _trello_list_ids["done"] = lst["id"]

        # Create any lists that weren't found on the board
        needed = [
            ("todo", "To Do"),
            ("in_progress", "In Progress"),
            ("done", "Done"),
        ]
        async with aiohttp.ClientSession() as session:
            for key, display_name in needed:
                if not _trello_list_ids[key]:
                    async with session.post(
                        f"{TRELLO_BASE}/lists",
                        params=_trello_params(name=display_name, idBoard=TRELLO_BOARD_ID),
                    ) as resp:
                        data = await resp.json()
                        _trello_list_ids[key] = data["id"]
                        print(f"[TRELLO] Created list '{display_name}' → {data['id']}")

        print(f"[TRELLO] List IDs: {_trello_list_ids}")

    except Exception as e:
        print(f"[TRELLO] Init error: {e}")


async def trello_create_card(task_id: int, description: str, priority: str = "Normal") -> str | None:
    """Create a card in the To Do list. Returns the new card ID."""
    if not _trello_enabled() or not _trello_list_ids["todo"]:
        return None
    try:
        card_desc = f"**Priority:** {priority}\n**Progress:** 0%\n\n*Managed by herbsbot*"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{TRELLO_BASE}/cards",
                params=_trello_params(
                    idList=_trello_list_ids["todo"],
                    name=f"[#{task_id}] {description}",
                    desc=card_desc,
                    pos="bottom",
                ),
            ) as resp:
                data = await resp.json()
                card_id = data.get("id")
                print(f"[TRELLO] Created card {card_id} for task #{task_id}")
                return card_id
    except Exception as e:
        print(f"[TRELLO] Error creating card: {e}")
        return None


async def trello_update_progress(task_id: int, percent: int, status: str = ""):
    """
    Update a card's description with the latest task fields,
    and move it to the correct list based on progress:
      0%      → To Do
      1–99%   → In Progress
      100%    → Done
    """
    if not _trello_enabled():
        return
    card_id = trello_map.get(task_id)
    if not card_id:
        return

    # Decide which list the card should live in
    if percent == 0:
        target_list = _trello_list_ids["todo"]
    elif percent < 100:
        target_list = _trello_list_ids["in_progress"]
    else:
        target_list = _trello_list_ids["done"]

    task = tasks_db.get(task_id, {})
    priority = task.get("priority", "Normal")
    assigned = task.get("assigned") or "—"
    card_status = status or task.get("status", "In progress")
    bar = "█" * max(1, percent // 10) + "░" * (10 - max(1, percent // 10))
    card_desc = (
        f"**Priority:** {priority}\n"
        f"**Assigned:** {assigned}\n"
        f"**Status:** {card_status}\n"
        f"**Progress:** [{bar}] {percent}%\n\n"
        f"*Managed by herbsbot*"
    )

    try:
        params = _trello_params(desc=card_desc)
        if target_list:
            params["idList"] = target_list
        async with aiohttp.ClientSession() as session:
            async with session.put(
                f"{TRELLO_BASE}/cards/{card_id}",
                params=params,
            ) as resp:
                if resp.status == 200:
                    print(f"[TRELLO] Updated card {card_id} → {percent}%")
                else:
                    text = await resp.text()
                    print(f"[TRELLO] Update failed {resp.status}: {text}")
    except Exception as e:
        print(f"[TRELLO] Error updating card: {e}")


async def trello_complete_card(task_id: int):
    """
    Move a card to the Done list without archiving it.
    The task mapping is kept so the card remains accessible in Trello.
    """
    if not _trello_enabled():
        return
    card_id = trello_map.get(task_id)
    if not card_id:
        return
    try:
        params = _trello_params()
        if _trello_list_ids["done"]:
            params["idList"] = _trello_list_ids["done"]
        async with aiohttp.ClientSession() as session:
            async with session.put(
                f"{TRELLO_BASE}/cards/{card_id}",
                params=params,
            ) as resp:
                if resp.status == 200:
                    print(f"[TRELLO] Moved card {card_id} to Done for task #{task_id}")
                else:
                    text = await resp.text()
                    print(f"[TRELLO] Move to Done failed {resp.status}: {text}")
    except Exception as e:
        print(f"[TRELLO] Error completing card: {e}")
    # Mapping is intentionally kept — card still exists in Trello under Done


async def trello_archive_card(task_id: int):
    """
    Archive (close) a card in Trello and remove its local mapping.
    Used when a task is permanently removed via !task_remove.
    """
    if not _trello_enabled():
        return
    card_id = trello_map.get(task_id)
    if not card_id:
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.put(
                f"{TRELLO_BASE}/cards/{card_id}",
                params=_trello_params(closed="true"),
            ) as resp:
                if resp.status == 200:
                    print(f"[TRELLO] Archived card {card_id} for removed task #{task_id}")
                else:
                    text = await resp.text()
                    print(f"[TRELLO] Archive failed {resp.status}: {text}")
    except Exception as e:
        print(f"[TRELLO] Error archiving card: {e}")
    # Clean up the mapping since the card is gone
    trello_map.pop(task_id, None)
    save_trello_map()


async def trello_add_comment(task_id: int, text: str):
    """Post a comment on the Trello card."""
    if not _trello_enabled():
        return
    card_id = trello_map.get(task_id)
    if not card_id:
        return
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{TRELLO_BASE}/cards/{card_id}/actions/comments",
                params=_trello_params(text=text),
            )
    except Exception as e:
        print(f"[TRELLO] Error adding comment: {e}")


# ==========================================================
#  Ollama AI Helper
# ==========================================================

async def ask_ollama(prompt: str, system: str = "") -> str:
    """Send a prompt to the local Ollama instance and return the response text."""
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
        print(f"[OLLAMA] Error: {e}")
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
- task_remove: { "action": "task_remove", "id": <number> }
- task_progress: { "action": "task_progress", "id": <number>, "percent": <0-100> }
- task_status: { "action": "task_status", "id": <number>, "status": "..." }
- task_priority: { "action": "task_priority", "id": <number>, "level": "..." }
- task_assign: { "action": "task_assign", "id": <number>, "assignee": "..." }
- task_info: { "action": "task_info", "id": <number> }
- task_table: { "action": "task_table" }
- rest: { "action": "rest", "minutes": <1-30> }
- question: { "action": "question", "answer": "..." }
- unknown: { "action": "unknown" }

IMPORTANT RULES:
- task_done marks a task as completed (stays in the table, moves to Done in Trello).
- task_remove permanently deletes a task (removed from table, archived in Trello).
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
    Ask Ollama to check if a message contains inappropriate content.
    Returns { "flagged": true/false, "reason": "..." }
    Falls back to a simple keyword check if Ollama is unavailable.
    """
    system = """You are a content moderator for a professional team Discord server.
Analyze the message and return ONLY a valid JSON object:
{ "flagged": true/false, "reason": "short reason or empty string" }

Flag messages that contain: profanity, insults, harassment, hate speech, or highly unprofessional language.
Do NOT flag: frustration, mild venting, technical jargon, or normal work conversation.
Return ONLY the JSON object."""

    response = await ask_ollama(f'Message: "{text}"', system)
    if not response:
        # Fallback to simple keyword check
        bad_words = ["shit", "fuck", "ass", "damn", "bitch", "crap"]
        flagged = any(w in text.lower() for w in bad_words)
        return {"flagged": flagged, "reason": "profanity detected" if flagged else ""}

    response = response.strip().strip("```json").strip("```").strip()
    try:
        return json.loads(response)
    except Exception:
        return {"flagged": False, "reason": ""}


async def generate_checkin_reply(username: str, reply_text: str) -> str:
    """Generate a warm, encouraging reply to a check-in message."""
    system = """You are a friendly and supportive team bot for a small company.
A team member just replied to their check-in. Write a short (1-2 sentence), warm, and encouraging response.
Be genuine, not robotic. Vary your responses. Don't use emojis excessively."""

    response = await ask_ollama(f'{username} said: "{reply_text}"', system)
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
        print("[DEBUG] No existing tasks file — starting fresh.")
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


async def complete_task(channel, tid, who):
    """
    Mark a task as 100% completed.
    The task stays in the table and moves to Done in Trello.
    """
    t = tasks_db.get(tid)
    if t:
        t["progress"] = 100
        t["status"] = "Completed"
        save_tasks()
        await trello_complete_card(tid)
        await channel.send(
            f"🎉 Great job {who}! Task #{tid} **{t['desc']}** marked as completed 👏"
        )


async def remove_task(channel, tid):
    """
    Permanently delete a task from the table and archive it in Trello.
    Use complete_task() instead if you just want to mark it done.
    """
    t = tasks_db.pop(tid, None)
    if t:
        save_tasks()
        await trello_archive_card(tid)
        await channel.send(f"🗑️ Task #{tid} **{t['desc']}** has been removed.")
    else:
        await channel.send(f"❌ Task #{tid} not found.")


def format_task_table() -> str:
    if not tasks_db:
        return "No active tasks ✅"
    header = (
        "ID | Prog | Status          | Priority | Assignee  | Description\n"
        "---|------|-----------------|----------|-----------|------------"
    )
    rows = [header]
    for i, t in tasks_db.items():
        trello_note = "🟦" if i in trello_map else "  "
        rows.append(
            f"{i:<2} | {t['progress']:>3}% | {t['status'][:15]:<15} "
            f"| {t['priority'][:8]:<8} | {t['assigned'] or '—':<9} | {trello_note} {t['desc'][:48]}"
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
            await channel.send("I couldn't figure out the task description. Could you rephrase?")
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
        # Sync to Trello
        card_id = await trello_create_card(tid, desc)
        if card_id:
            trello_map[tid] = card_id
            save_trello_map()
            await channel.send(f"🆕 Task #{tid} created: *{desc}* 🟦 (synced to Trello)")
        else:
            await channel.send(f"🆕 Task #{tid} created: *{desc}*")

    elif action == "task_done":
        tid = int(intent.get("id", 0))
        if tid not in tasks_db:
            await channel.send(f"❌ I couldn't find task #{tid}.")
            return
        await complete_task(channel, tid, tasks_db[tid].get("assigned") or author.display_name)

    elif action == "task_remove":
        tid = int(intent.get("id", 0))
        await remove_task(channel, tid)

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
        await trello_update_progress(tid, percent)
        await channel.send(f"📈 Task #{tid}: [{bar}] {percent}%")
        if percent == 100:
            await complete_task(channel, tid, t["assigned"] or author.display_name)

    elif action == "task_status":
        tid = int(intent.get("id", 0))
        status = intent.get("status", "").strip()
        if tid not in tasks_db:
            await channel.send(f"❌ I couldn't find task #{tid}.")
            return
        tasks_db[tid]["status"] = status
        save_tasks()
        await trello_update_progress(tid, tasks_db[tid]["progress"], status)
        await channel.send(f"ℹ️ Task #{tid} status → **{status}**")

    elif action == "task_priority":
        tid = int(intent.get("id", 0))
        level = intent.get("level", "Normal").capitalize()
        if tid not in tasks_db:
            await channel.send(f"❌ I couldn't find task #{tid}.")
            return
        tasks_db[tid]["priority"] = level
        save_tasks()
        await trello_update_progress(tid, tasks_db[tid]["progress"])
        await channel.send(f"🔥 Task #{tid} priority → **{level}**")

    elif action == "task_assign":
        tid = int(intent.get("id", 0))
        assignee = intent.get("assignee", "").strip()
        if tid not in tasks_db:
            await channel.send(f"❌ I couldn't find task #{tid}.")
            return
        tasks_db[tid]["assigned"] = assignee
        save_tasks()
        await trello_update_progress(tid, tasks_db[tid]["progress"])
        await trello_add_comment(tid, f"Assigned to: {assignee}")
        await channel.send(f"👥 Task #{tid} assigned to **{assignee}**")

    elif action == "task_info":
        tid = int(intent.get("id", 0))
        if tid not in tasks_db:
            await channel.send(f"❌ I couldn't find task #{tid}.")
            return
        t = tasks_db[tid]
        trello_note = "\n🟦 Trello card linked" if tid in trello_map else ""
        await channel.send(
            f"📝 **Task #{tid}**\n"
            f"Desc: {t['desc']}\n"
            f"Progress: [{progress_bar(t['progress'])}] {t['progress']}%\n"
            f"Status: {t['status']}\n"
            f"Priority: {t['priority']}\n"
            f"Assigned to: {t['assigned'] or '—'}"
            f"{trello_note}"
        )

    elif action == "task_table":
        await channel.send(format_task_table())

    elif action == "rest":
        minutes = max(1, min(int(intent.get("minutes", 5)), 30))
        user = author.id
        if user in active_timers:
            await channel.send(f"⏳ {author.mention}, you already have a timer running!")
            return
        await channel.send(f"🕓 Starting a {minutes}-minute break for {author.mention} ☕")
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

    else:
        await channel.send(
            "I'm not sure what you mean. You can talk to me naturally — try something like:\n"
            '• *"add a task to review the website"*\n'
            '• *"mark task 3 as done"*\n'
            '• *"remove task 5"*\n'
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
    load_trello_map()
    await _trello_init()

    # on_ready can fire multiple times on reconnect — guard with is_running()
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
        with open(CHECKIN_FILE, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                datetime.now().isoformat(timespec="seconds"),
                message.author.id,
                message.author.display_name,
                reply_text,
            ])

        # AI-generated reply
        ai_reply = await generate_checkin_reply(message.author.display_name, reply_text)
        await message.channel.send(ai_reply)

        # Forward reply to check-in-responses channel (falls back to general)
        if bot.guilds:
            guild = bot.guilds[0]
            chan = discord.utils.get(guild.text_channels, name="check-in-responses") \
                or discord.utils.get(guild.text_channels, name="general")
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
    # Only respond if the bot is mentioned or the message is a reply to the bot
    bot_mentioned = bot.user in message.mentions
    is_reply_to_bot = (
        message.reference
        and message.reference.resolved
        and getattr(message.reference.resolved, "author", None) == bot.user
    )

    if bot_mentioned or is_reply_to_bot:
        # Strip the mention from the message before parsing
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
#  Legacy ! Commands (still work as backup alongside natural language)
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
    # Sync to Trello
    card_id = await trello_create_card(tid, description)
    if card_id:
        trello_map[tid] = card_id
        save_trello_map()
        await ctx.send(f"🆕 Task #{tid} created: *{description}* 🟦")
    else:
        await ctx.send(f"🆕 Task #{tid} created: *{description}*")


@bot.command()
async def task_done(ctx, tid: int):
    """Mark a task as completed (stays in table, moves to Done in Trello)."""
    if tid not in tasks_db:
        await ctx.send("❌ Task not found.")
        return
    await complete_task(ctx.channel, tid, tasks_db[tid]["assigned"] or ctx.author.display_name)


@bot.command()
async def task_remove(ctx, tid: int):
    """Permanently remove a task (deleted from table, archived in Trello)."""
    await remove_task(ctx.channel, tid)


@bot.command()
async def task_progress(ctx, tid: int, percent: int):
    """Update the progress percentage of a task."""
    if tid not in tasks_db:
        await ctx.send("❌ Task not found.")
        return
    percent = max(0, min(100, percent))
    t = tasks_db[tid]
    t["progress"] = percent
    save_tasks()
    await trello_update_progress(tid, percent)
    await ctx.send(f"📈 Task #{tid}: [{progress_bar(percent)}] {percent}%")
    if percent == 100:
        await complete_task(ctx.channel, tid, t["assigned"] or ctx.author.display_name)


@bot.command()
async def task_status(ctx, tid: int, *, new_status: str):
    """Update the status label of a task."""
    if tid not in tasks_db:
        await ctx.send("❌ Task not found.")
        return
    tasks_db[tid]["status"] = new_status
    save_tasks()
    await trello_update_progress(tid, tasks_db[tid]["progress"], new_status)
    await ctx.send(f"ℹ️ Task #{tid} status → **{new_status}**")


@bot.command()
async def task_priority(ctx, tid: int, *, level: str):
    """Set the priority level of a task."""
    if tid not in tasks_db:
        await ctx.send("❌ Task not found.")
        return
    tasks_db[tid]["priority"] = level.capitalize()
    save_tasks()
    await trello_update_progress(tid, tasks_db[tid]["progress"])
    await ctx.send(f"🔥 Task #{tid} priority → **{level.capitalize()}**")


@bot.command()
async def task_assign(ctx, tid: int, member: discord.Member):
    """Assign a task to a server member."""
    if tid not in tasks_db:
        await ctx.send("❌ Task not found.")
        return
    tasks_db[tid]["assigned"] = member.display_name
    save_tasks()
    await trello_update_progress(tid, tasks_db[tid]["progress"])
    await trello_add_comment(tid, f"Assigned to: {member.display_name}")
    await ctx.send(f"👥 Task #{tid} assigned to **{member.display_name}**")


@bot.command()
async def task_info(ctx, tid: int):
    """Show full details for a single task."""
    if tid not in tasks_db:
        await ctx.send("❌ Task not found.")
        return
    t = tasks_db[tid]
    trello_note = "\n🟦 Trello card linked" if tid in trello_map else ""
    await ctx.send(
        f"📝 **Task #{tid}**\n"
        f"Desc: {t['desc']}\n"
        f"Progress: [{progress_bar(t['progress'])}] {t['progress']}%\n"
        f"Status: {t['status']}\n"
        f"Priority: {t['priority']}\n"
        f"Assigned to: {t['assigned'] or '—'}"
        f"{trello_note}"
    )


@bot.command()
async def task_table(ctx):
    """Show all tasks in a table."""
    await ctx.send(format_task_table())


# ==========================================================
#  ☕ Rest Break Command
# ==========================================================

@bot.command()
async def rest(ctx, minutes: int = 5):
    """Start a timed break (1–30 min, default 5)."""
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
    """Show the bot's current time and UTC."""
    lj = datetime.now(TIMEZONE)
    utc = discord.utils.utcnow()
    await ctx.send(
        "```"
        f"Bot TZ : {lj.isoformat(timespec='seconds')}\n"
        f"UTC    : {utc.isoformat(timespec='seconds')}\n"
        "```"
    )


# ==========================================================
#  Work‑Hour Reminders
# ==========================================================

@bot.command()
async def setnotify(ctx):
    """Set this channel to receive hourly work-hour reminders."""
    global notify_channel_id
    notify_channel_id = ctx.channel.id
    await ctx.send("📅 Work‑hour reminders will appear here!")


# ==========================================================
#  EOD Test Command
# ==========================================================

@bot.command()
async def test_eod(ctx):
    """Manually trigger the end-of-day reminder for testing."""
    await ctx.send("⏰ Triggering EOD reminder now...")
    await _send_eod_reminders()
    await ctx.send("✅ Done. Check DMs and logs.")


# ==========================================================
#  EOD Reminder Logic
# ==========================================================

async def _send_eod_reminders():
    """
    DM all members who hold a role in TARGET_ROLE_IDS, asking them to post a
    WhatsApp summary of their day. Called by the scheduled loop and !test_eod.
    """
    text = (
        "Hello!\n"
        "The workday has ended — please send a short summary in WhatsApp of what you worked on today "
        "(key tasks + current status). Thank you!"
    )
    print(f"[EOD] Guilds visible to bot: {[g.name for g in bot.guilds]}")
    for guild in bot.guilds:
        print(f"[EOD] Processing guild: {guild.name} ({guild.id})")
        try:
            await asyncio.wait_for(guild.chunk(), timeout=10.0)
            print(f"[EOD] Chunk complete. Member count: {guild.member_count}")
        except asyncio.TimeoutError:
            print("[EOD] Chunk timed out — using cached members instead.")
        except Exception as e:
            print(f"[EOD] Chunk failed: {e}")

        # Collect unique members across all target roles (avoid duplicate DMs)
        targets = {}
        for rid in TARGET_ROLE_IDS:
            role = guild.get_role(rid)
            if not role:
                print(f"[EOD] Role {rid} NOT FOUND in {guild.name}")
                continue
            print(f"[EOD] Role '{role.name}' has {len(role.members)} members")
            for m in role.members:
                if not m.bot:
                    targets[m.id] = m

        print(f"[EOD] Total unique targets: {len(targets)}")
        for member in targets.values():
            try:
                await member.send(text)
                print(f"[EOD] ✅ Sent to {member.display_name}")
                await asyncio.sleep(0.8)  # small delay to avoid rate limits
            except discord.Forbidden:
                print(f"[EOD] ❌ Forbidden — {member.display_name} has DMs closed")
            except Exception as e:
                print(f"[EOD] ❌ Failed to DM {member.display_name}: {e}")


# ==========================================================
#  Scheduled Tasks
# ==========================================================

@tasks.loop(minutes=60)
async def workday_reminder():
    """Post a work reminder in the notify channel every hour during business hours."""
    if not notify_channel_id:
        return
    now = datetime.now(TIMEZONE)
    if now.weekday() < 5 and 9 <= now.hour <= 17:
        channel = bot.get_channel(notify_channel_id)
        if channel:
            await channel.send("☀️ Good day team! Check `!task_table` and stay focused 💪")


@workday_reminder.before_loop
async def before_workday_reminder():
    await bot.wait_until_ready()
    print("[DEBUG] Workday reminder loop started.")


# ==========================================================
#  🌿 Automatic User Check‑Ins
# ==========================================================

@tasks.loop(minutes=CHECKIN_INTERVAL)
async def user_checkin():
    """DM each online member a quick check-in question."""
    print("[DEBUG] Running user_checkin loop…")
    for guild in bot.guilds:
        try:
            await asyncio.wait_for(guild.chunk(), timeout=10.0)
        except asyncio.TimeoutError:
            print(f"[DEBUG] Chunk timed out for guild: {guild.name}")
        except Exception as e:
            print(f"[DEBUG] Chunk failed for guild {guild.name}: {e}")

        for member in guild.members:
            if member.bot or member.status == discord.Status.offline:
                continue
            try:
                await member.send(
                    f"🌿 Hey {member.display_name}! "
                    "How are you doing right now, and what are you working on?"
                )
                await asyncio.sleep(2)  # small delay between DMs
            except discord.Forbidden:
                # Fallback to general channel if member has DMs closed
                channel = discord.utils.get(guild.text_channels, name="general")
                if channel:
                    await channel.send(f"{member.mention} 🌿 How are you doing right now?")
                await asyncio.sleep(2)


@user_checkin.before_loop
async def before_user_checkin():
    await bot.wait_until_ready()


# ==========================================================
#  ✅ End-of-day WhatsApp Reminder (15:58 Nicosia time, Mon–Fri)
# ==========================================================

@tasks.loop(minutes=1)
async def end_of_day_whatsapp_reminder():
    """Polls every minute and fires the EOD reminder at the configured time on weekdays."""
    now = datetime.now(TIMEZONE)
    if now.weekday() >= 5:  # 5 = Sat, 6 = Sun
        return
    if not (now.hour == END_OF_DAY_HOUR and now.minute == END_OF_DAY_MINUTE):
        return  # Not time yet
    print(f"[EOD] Scheduled trigger at {now.isoformat()}")
    await _send_eod_reminders()


@end_of_day_whatsapp_reminder.before_loop
async def before_end_of_day_whatsapp_reminder():
    await bot.wait_until_ready()
    print("[DEBUG] end_of_day_whatsapp_reminder loop started.")


# ==========================================================
#  Run
# ==========================================================

bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)