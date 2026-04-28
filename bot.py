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

import workflow_manager
import streaming_monitor
import vdmonitor_listener

# ==========================================================
#  Setup
# ==========================================================

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_int_list(name: str, default: list[int]) -> list[int]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default

    parsed: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            parsed.append(int(part))
        except ValueError:
            continue

    return parsed or default


# Define data directory
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)

# Update file paths
TASK_FILE = os.path.join(DATA_DIR, "tasks.json")
CHECKIN_FILE = os.path.join(DATA_DIR, "checkins.csv")

handler = logging.FileHandler(
    filename=os.path.join(DATA_DIR, "discord.log"), encoding="utf-8", mode="w"
)
logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s"
)

intents = discord.Intents.default()
intents.message_content = True
intents.members = (
    True  # Also enable "SERVER MEMBERS INTENT" in the Discord Developer Portal
)

bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================================================
#  Persistent Files & Globals
# ==========================================================

OLLAMA_URL = os.getenv("OLLAMA_URL")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL")

tasks_db = {}  # { task_id: task_dict } — loaded from disk on startup
task_counter = 1  # auto-increments with each new task
notify_channel_id = None  # set via !setnotify; used for hourly work reminders
active_timers = {}  # { user_id: True } — tracks in-progress break timers

CHECKIN_INTERVAL = _env_int(
    "CHECKIN_INTERVAL", 60
)  # minutes between automatic check-in DMs

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
    print(
        "[WARNING] Could not load Nicosia timezone. Falling back to local machine timezone."
    )
    print("[WARNING] Install the tzdata package to fix this.")
    print(f"[WARNING] Last error: {last_err}")
    return local_tz


TIMEZONE = load_timezone()

# End-of-day reminder time (15:58 Nicosia local time). Adjust as needed.
END_OF_DAY_HOUR = _env_int("END_OF_DAY_HOUR", 15)
END_OF_DAY_MINUTE = _env_int("END_OF_DAY_MINUTE", 58)

# Role IDs that receive the end-of-day WhatsApp summary reminder
TARGET_ROLE_IDS = _env_int_list(
    "TARGET_ROLE_IDS",
    [],
)


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
            elif (
                name_lower in in_progress_names and not _trello_list_ids["in_progress"]
            ):
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
                        params=_trello_params(
                            name=display_name, idBoard=TRELLO_BOARD_ID
                        ),
                    ) as resp:
                        data = await resp.json()
                        _trello_list_ids[key] = data["id"]
                        print(f"[TRELLO] Created list '{display_name}' → {data['id']}")

        print(f"[TRELLO] List IDs: {_trello_list_ids}")

    except Exception as e:
        print(f"[TRELLO] Init error: {e}")


async def trello_create_card(
    task_id: int, description: str, priority: str = "Normal"
) -> str | None:
    """Create a card in the To Do list. Returns the new card ID."""
    if not _trello_enabled() or not _trello_list_ids["todo"]:
        return None
    try:
        card_desc = (
            f"**Priority:** {priority}\n**Progress:** 0%\n\n*Managed by herbsbot*"
        )
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
                    print(
                        f"[TRELLO] Archived card {card_id} for removed task #{task_id}"
                    )
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
#  🟡 Miro Integration
# ==========================================================

MIRO_ACCESS_TOKEN = os.getenv("MIRO_ACCESS_TOKEN", "")
MIRO_BOARD_ID = os.getenv("MIRO_BOARD_ID", "")
MIRO_BASE = "https://api.miro.com/v2"

# Frame IDs for organizing sticky notes (discovered/created on startup)
_miro_frame_ids = {
    "todo": None,
    "in_progress": None,
    "done": None,
}

# # Position tracking for sticky notes within each frame
# _miro_frame_positions = {
#     "todo": {"x": 0, "y": 0},
#     "in_progress": {"x": 600, "y": 0},
#     "done": {"x": 1200, "y": 0},
# }

# Maps internal task ID → Miro item ID; persisted to disk
MIRO_MAP_FILE = os.path.join(DATA_DIR, "miro_map.json")
miro_map: dict[int, str] = {}  # { task_id: miro_item_id }

# Priority → Miro sticky note color mapping
MIRO_PRIORITY_COLORS = {
    "critical": "red",
    "high": "orange",
    "normal": "yellow",
    "low": "green",
    "lowest": "blue",
}


def _miro_enabled() -> bool:
    return bool(MIRO_ACCESS_TOKEN and MIRO_BOARD_ID)


def _miro_headers() -> dict:
    """Build headers for Miro API requests."""
    return {
        "Authorization": f"Bearer {MIRO_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def load_miro_map():
    global miro_map
    try:
        with open(MIRO_MAP_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        miro_map = {int(k): v for k, v in raw.items()}
        print(f"[MIRO] Loaded {len(miro_map)} item mappings.")
    except FileNotFoundError:
        miro_map = {}
    except Exception as e:
        print(f"[MIRO] Error loading miro_map: {e}")
        miro_map = {}


def save_miro_map():
    try:
        with open(MIRO_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(miro_map, f, indent=2)
    except Exception as e:
        print(f"[MIRO] Error saving miro_map: {e}")


async def _miro_init():
    """
    On startup, discover existing frames on the board by name.
    Creates any missing frames automatically.
    """
    if not _miro_enabled():
        print("[MIRO] Env vars not set — Miro sync disabled.")
        return

    url = f"{MIRO_BASE}/boards/{MIRO_BOARD_ID}/items"
    params = {"type": "frame", "limit": 50}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_miro_headers(), params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    print(f"[MIRO] Failed to fetch frames: {resp.status} - {text}")
                    return
                data = await resp.json()

        frames = data.get("data", [])

        # Map frame names to our categories
        todo_names = {"to do", "todo", "backlog"}
        in_progress_names = {"in progress", "doing", "wip"}
        done_names = {"done", "completed", "finished"}

        for frame in frames:
            title = (frame.get("data", {}).get("title") or "").lower()
            frame_id = frame.get("id")
            # position = frame.get("position", {})

            if title in todo_names and not _miro_frame_ids["todo"]:
                _miro_frame_ids["todo"] = frame_id
                # _miro_frame_positions["todo"] = {"x": position.get("x", 0), "y": position.get("y", 0)}
            elif title in in_progress_names and not _miro_frame_ids["in_progress"]:
                _miro_frame_ids["in_progress"] = frame_id
                # _miro_frame_positions["in_progress"] = {"x": position.get("x", 600), "y": position.get("y", 0)}
            elif title in done_names and not _miro_frame_ids["done"]:
                _miro_frame_ids["done"] = frame_id
                # _miro_frame_positions["done"] = {"x": position.get("x", 1200), "y": position.get("y", 0)}

        # Create any missing frames
        needed = [
            ("todo", "To Do", 0),
            ("in_progress", "In Progress", 600),
            ("done", "Done", 1200),
        ]

        async with aiohttp.ClientSession() as session:
            for key, title, x_pos in needed:
                if not _miro_frame_ids[key]:
                    frame_data = {
                        "data": {
                            "title": title,
                            # "format": "custom",
                            # "width": 500,
                            # "height": 800,
                        },
                        "position": {
                            "x": x_pos,
                            "y": 0,
                            "origin": "center",
                        },
                    }
                    async with session.post(
                        f"{MIRO_BASE}/boards/{MIRO_BOARD_ID}/frames",
                        headers=_miro_headers(),
                        json=frame_data,
                    ) as resp:
                        if resp.status in (200, 201):
                            result = await resp.json()
                            _miro_frame_ids[key] = result.get("id")
                            # _miro_frame_positions[key] = {"x": x_pos, "y": 0}
                            print(
                                f"[MIRO] Created frame '{title}' → {result.get('id')}"
                            )
                        else:
                            text = await resp.text()
                            print(
                                f"[MIRO] Failed to create frame '{title}': {resp.status} - {text}"
                            )

        print(f"[MIRO] Frame IDs: {_miro_frame_ids}")

    except Exception as e:
        print(f"[MIRO] Init error: {e}")


def _get_sticky_color(priority: str) -> str:
    """Map priority level to Miro sticky note color."""
    return MIRO_PRIORITY_COLORS.get(priority.lower(), "yellow")


def _build_sticky_content(task_id: int, task: dict) -> str:
    """Build the text content for a Miro sticky note."""
    desc = task.get("desc", "")
    progress = task.get("progress", 0)
    status = task.get("status", "Not started")
    assigned = task.get("assigned") or "Unassigned"
    priority = task.get("priority", "Normal")

    return (
        f"#{task_id}: {desc}\n"
        f"━━━━━━━━━━\n"
        f"📊 {progress}%\n"
        f"📌 {status}\n"
        f"👤 {assigned}\n"
        f"🔥 {priority}"
    )


async def miro_create_sticky(
    task_id: int, description: str, priority: str = "Normal"
) -> str | None:
    """Create a sticky note in the To Do frame. Returns the new item ID."""
    if not _miro_enabled() or not _miro_frame_ids["todo"]:
        return None

    try:
        task = tasks_db.get(
            task_id,
            {
                "desc": description,
                "progress": 0,
                "status": "Not started",
                "priority": priority,
                "assigned": None,
            },
        )

        # Calculate position within the frame
        # frame_pos = _miro_frame_positions["todo"]
        # Stack sticky notes vertically, offset from frame center
        # existing_in_frame = sum(1 for tid, _ in miro_map.items()
        # if tasks_db.get(tid, {}).get("progress", 0) == 0)

        sticky_data = {
            "data": {
                "content": _build_sticky_content(task_id, task),
                "shape": "square",
            },
            "style": {
                "fillColor": _get_sticky_color(priority),
            },
            "position": {
                # "x": frame_pos["x"],
                # "y": frame_pos["y"] + (existing_in_frame * 120) - 300,
                "x": 0,
                "y": 0,
                "origin": "center",
            },
            "parent": {
                "id": _miro_frame_ids["todo"],
            },
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{MIRO_BASE}/boards/{MIRO_BOARD_ID}/sticky_notes",
                headers=_miro_headers(),
                json=sticky_data,
            ) as resp:
                if resp.status in (200, 201):
                    result = await resp.json()
                    item_id = result.get("id")
                    print(f"[MIRO] Created sticky {item_id} for task #{task_id}")
                    return item_id
                else:
                    text = await resp.text()
                    print(f"[MIRO] Failed to create sticky: {resp.status} - {text}")
                    return None

    except Exception as e:
        print(f"[MIRO] Error creating sticky: {e}")
        return None


async def miro_update_sticky(task_id: int, percent: int = None, status: str = ""):
    """
    Update a sticky note's content and move it to the correct frame.
    """
    if not _miro_enabled():
        return

    item_id = miro_map.get(task_id)
    if not item_id:
        return

    task = tasks_db.get(task_id, {})
    if percent is not None:
        task["progress"] = percent

    # Determine target frame based on progress
    progress = task.get("progress", 0)
    if progress == 0:
        target_frame = "todo"
    elif progress < 100:
        target_frame = "in_progress"
    else:
        target_frame = "done"

    target_frame_id = _miro_frame_ids.get(target_frame)

    try:
        # Update sticky note content and color
        update_data = {
            "data": {
                "content": _build_sticky_content(task_id, task),
            },
            "style": {
                "fillColor": _get_sticky_color(task.get("priority", "Normal")),
            },
        }

        # If we have a target frame, include parent to move it
        if target_frame_id:
            update_data["parent"] = {"id": target_frame_id}
            # frame_pos = _miro_frame_positions[target_frame]
            update_data["position"] = {
                # "x": frame_pos["x"],
                # "y": frame_pos["y"],
                "x": 0,
                "y": 0,
                "origin": "center",
            }

        async with aiohttp.ClientSession() as session:
            async with session.patch(
                f"{MIRO_BASE}/boards/{MIRO_BOARD_ID}/sticky_notes/{item_id}",
                headers=_miro_headers(),
                json=update_data,
            ) as resp:
                if resp.status == 200:
                    print(f"[MIRO] Updated sticky {item_id} → {progress}%")
                else:
                    text = await resp.text()
                    print(f"[MIRO] Update failed {resp.status}: {text}")

    except Exception as e:
        print(f"[MIRO] Error updating sticky: {e}")


async def miro_complete_sticky(task_id: int):
    """Move a sticky note to the Done frame."""
    if not _miro_enabled():
        return

    item_id = miro_map.get(task_id)
    if not item_id:
        return

    await miro_update_sticky(task_id, percent=100)


async def miro_delete_sticky(task_id: int):
    """Delete a sticky note from Miro."""
    if not _miro_enabled():
        return

    item_id = miro_map.get(task_id)
    if not item_id:
        return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.delete(
                f"{MIRO_BASE}/boards/{MIRO_BOARD_ID}/sticky_notes/{item_id}",
                headers=_miro_headers(),
            ) as resp:
                if resp.status in (200, 204):
                    print(f"[MIRO] Deleted sticky {item_id} for task #{task_id}")
                else:
                    text = await resp.text()
                    print(f"[MIRO] Delete failed {resp.status}: {text}")

    except Exception as e:
        print(f"[MIRO] Error deleting sticky: {e}")

    # Clean up mapping
    miro_map.pop(task_id, None)
    save_miro_map()


# async def miro_add_tag(task_id: int, tag_text: str):
#     """
#     Add a tag to a sticky note (using Miro's tag feature).
#     Note: Tags in Miro are board-level, so we create/reuse them.
#     """
#     if not _miro_enabled():
#         return

#     item_id = miro_map.get(task_id)
#     if not item_id:
#         return

#     try:
#         # First, try to find or create the tag
#         async with aiohttp.ClientSession() as session:
#             # Get existing tags
#             async with session.get(
#                 f"{MIRO_BASE}/boards/{MIRO_BOARD_ID}/tags",
#                 headers=_miro_headers(),
#             ) as resp:
#                 tags_data = await resp.json()

#             existing_tags = tags_data.get("data", [])
#             tag_id = None

#             for tag in existing_tags:
#                 if tag.get("title", "").lower() == tag_text.lower():
#                     tag_id = tag.get("id")
#                     break

#             # Create tag if it doesn't exist
#             if not tag_id:
#                 async with session.post(
#                     f"{MIRO_BASE}/boards/{MIRO_BOARD_ID}/tags",
#                     headers=_miro_headers(),
#                     json={"title": tag_text, "fillColor": "blue"},
#                 ) as resp:
#                     if resp.status in (200, 201):
#                         tag_result = await resp.json()
#                         tag_id = tag_result.get("id")

#             # Attach tag to item
#             if tag_id:
#                 async with session.post(
#                     f"{MIRO_BASE}/boards/{MIRO_BOARD_ID}/items/{item_id}/tags",
#                     headers=_miro_headers(),
#                     json={"tagId": tag_id},
#                 ) as resp:
#                     if resp.status in (200, 201):
#                         print(f"[MIRO] Added tag '{tag_text}' to sticky {item_id}")

#     except Exception as e:
#         print(f"[MIRO] Error adding tag: {e}")


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
    Syncs to Miro as well.
    """
    t = tasks_db.get(tid)
    if t:
        t["progress"] = 100
        t["status"] = "Completed"
        save_tasks()
        await trello_complete_card(tid)
        await miro_complete_sticky(tid)
        await channel.send(
            f"🎉 Great job {who}! Task #{tid} **{t['desc']}** marked as completed 👏"
        )


async def remove_task(channel, tid):
    """
    Permanently delete a task from the table and archive it in Trello.
    Use complete_task() instead if you just want to mark it done.
    Removes from Miro as well.
    """
    t = tasks_db.pop(tid, None)
    if t:
        save_tasks()
        await trello_archive_card(tid)
        await miro_delete_sticky(tid)
        await channel.send(f"🗑️ Task #{tid} **{t['desc']}** has been removed.")
    else:
        await channel.send(f"❌ Task #{tid} not found.")


def format_task_table() -> str:
    if not tasks_db:
        return "No active tasks ✅"
    header = (
        "ID | Prog | Status          | Priority | Assignee  | Sync | Description\n"
        "---|------|-----------------|----------|-----------|------|------------"
    )
    rows = [header]
    for i, t in tasks_db.items():
        sync_icons = ""
        if i in trello_map:
            sync_icons += "🟦"
        if i in miro_map:
            sync_icons += "🟡"
        sync_icons = sync_icons or "—"
        rows.append(
            f"{i:<2} | {t['progress']:>3}% | {t['status'][:15]:<15} "
            f"| {t['priority'][:8]:<8} | {t['assigned'] or '—':<9} | {sync_icons:<4} | {t['desc'][:40]}"
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

        # Sync to Trello
        card_id = await trello_create_card(tid, desc)
        if card_id:
            trello_map[tid] = card_id
            save_trello_map()

        # Sync to Miro
        miro_id = await miro_create_sticky(tid, desc)
        if miro_id:
            miro_map[tid] = miro_id
            save_miro_map()

        # Build response with sync indicators
        sync_icons = []
        if card_id:
            sync_icons.append("🟦 Trello")
        if miro_id:
            sync_icons.append("🟡 Miro")
        sync_text = f" ({', '.join(sync_icons)})" if sync_icons else ""

        # Send response
        await channel.send(f"🆕 Task #{tid} created: *{desc}*{sync_text}")

    elif action == "task_done":
        tid = int(intent.get("id", 0))
        if tid not in tasks_db:
            await channel.send(f"❌ I couldn't find task #{tid}.")
            return
        await complete_task(
            channel, tid, tasks_db[tid].get("assigned") or author.display_name
        )

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
        await miro_update_sticky(tid, percent)
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
        await miro_update_sticky(tid)
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
        await miro_update_sticky(tid)
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
        await miro_update_sticky(tid)
        # await miro_add_tag(tid, f"@{assignee}")
        await channel.send(f"👥 Task #{tid} assigned to **{assignee}**")

    elif action == "task_info":
        tid = int(intent.get("id", 0))
        if tid not in tasks_db:
            await channel.send(f"❌ I couldn't find task #{tid}.")
            return
        t = tasks_db[tid]
        sync_notes = []
        if tid in trello_map:
            sync_notes.append("🟦 Trello")
        if tid in miro_map:
            sync_notes.append("🟡 Miro")
        sync_text = "\n**Synced to:** " + ", ".join(sync_notes) if sync_notes else ""
        await channel.send(
            f"📝 **Task #{tid}**\n"
            f"Desc: {t['desc']}\n"
            f"Progress: [{progress_bar(t['progress'])}] {t['progress']}%\n"
            f"Status: {t['status']}\n"
            f"Priority: {t['priority']}\n"
            f"Assigned to: {t['assigned'] or '—'}"
            f"{sync_text}"
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
#  Workflow Intent Handler — routes natural language to workflow_manager
# ==========================================================


async def handle_workflow_intent(message: discord.Message, intent: dict):
    """Execute workflow-related intents parsed by workflow_manager."""
    action = intent.get("action", "")
    channel = message.channel
    author = message.author
    send = channel.send

    # --- Create a new workflow ---
    if action == "workflow_create":
        desc = intent.get("description", "").strip()
        if not desc:
            await send(
                "What should the workflow be about? Give me a short description."
            )
            return
        async with channel.typing():
            await workflow_manager.cmd_workflow_create(
                send_fn=send,
                description=desc,
                author_name=author.display_name,
            )

    # --- Edit an existing workflow ---
    elif action == "workflow_edit":
        wf_id = str(intent.get("id", "")).strip()
        edit = intent.get("edit", "").strip()

        if not wf_id:
            await send(
                "Which workflow do you want to edit? "
                "Please mention the ID "
                "(e.g. *edit workflow 2, add a review step*)."
            )
            await workflow_manager.cmd_workflow_list(send_fn=send)
            return

        if not edit:
            await send(f"What changes do you want to make to workflow #{wf_id}?")
            return

        async with channel.typing():
            await workflow_manager.cmd_workflow_edit(
                send_fn=send,
                wf_id=wf_id,
                edit_request=edit,
                author_name=author.display_name,
            )

    # --- Undo last edit ---
    elif action == "workflow_undo":
        wf_id = str(intent.get("id", "")).strip()
        if not wf_id:
            await send("Which workflow do you want to undo? Please provide the ID.")
            return
        async with channel.typing():
            await workflow_manager.cmd_workflow_undo(
                send_fn=send,
                wf_id=wf_id,
                author_name=author.display_name,
            )

    # --- List all workflows ---
    elif action == "workflow_list":
        await workflow_manager.cmd_workflow_list(send_fn=send)

    # --- View steps of a workflow ---
    elif action == "workflow_view":
        wf_id = str(intent.get("id", "")).strip()
        if not wf_id:
            await send("Which workflow do you want to view? Please provide the ID.")
            await workflow_manager.cmd_workflow_list(send_fn=send)
            return
        await workflow_manager.cmd_workflow_view(send_fn=send, wf_id=wf_id)

    # --- Delete a workflow ---
    elif action == "workflow_delete":
        wf_id = str(intent.get("id", "")).strip()
        if not wf_id:
            await send("Which workflow do you want to delete? Please provide the ID.")
            return
        await workflow_manager.cmd_workflow_delete(send_fn=send, wf_id=wf_id)

    # --- Force redraw on Miro ---
    elif action == "workflow_redraw":
        wf_id = str(intent.get("id", "")).strip()
        if not wf_id:
            await send("Which workflow do you want to redraw? Please provide the ID.")
            return
        async with channel.typing():
            await workflow_manager.cmd_workflow_redraw(send_fn=send, wf_id=wf_id)

    # --- Fallback ---
    else:
        await send(
            "I didn't quite understand that workflow request. Try something like:\n"
            '• *"create a workflow for client onboarding"*\n'
            '• *"add a review step after approval in workflow 1"*\n'
            '• *"undo the last change on workflow 1"*\n'
            '• *"show me all workflows"*\n'
            '• *"delete workflow 2"*'
        )


def _looks_like_workflow_request(text: str) -> bool:
    """Quick heuristic gate to avoid sending normal task messages to workflow AI parser."""
    lowered = text.lower()
    workflow_hints = [
        "workflow",
        "flowchart",
        "diagram",
        "miro",
        "wf ",
        "wf_",
        "redraw",
        "undo workflow",
        "edit workflow",
        "create workflow",
    ]
    return any(hint in lowered for hint in workflow_hints)


# ==========================================================
#  Events
# ==========================================================


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} — ID: {bot.user.id}")
    load_tasks()
    load_trello_map()
    load_miro_map()
    workflow_manager.load_workflows()
    streaming_monitor.setup(bot, TIMEZONE)
    vdmonitor_listener.set_bot(bot)
    asyncio.create_task(vdmonitor_listener.start_listener())
    await _trello_init()
    await _miro_init()

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
            csv.writer(f).writerow(
                [
                    datetime.now().isoformat(timespec="seconds"),
                    message.author.id,
                    message.author.display_name,
                    reply_text,
                ]
            )

        # AI-generated reply
        ai_reply = await generate_checkin_reply(message.author.display_name, reply_text)
        await message.channel.send(ai_reply)

        # Forward reply to check-in-responses channel (falls back to general)
        if bot.guilds:
            guild = bot.guilds[0]
            chan = discord.utils.get(
                guild.text_channels, name="check-in-responses"
            ) or discord.utils.get(guild.text_channels, name="general")
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
        text = message.content.replace(f"<@{bot.user.id}>", "").strip()
        if not text:
            await message.channel.send(
                "Hey! How can I help? Try asking me to add a task, "
                "check progress, or take a break."
            )
            return

        async with message.channel.typing():
            # Parse workflows only when the message explicitly looks workflow-related.
            if _looks_like_workflow_request(text):
                wf_intent = await workflow_manager.parse_workflow_intent(
                    text, message.author.display_name
                )
                if wf_intent:
                    await handle_workflow_intent(message, wf_intent)
                    return

            # Default path: task/rest/question intent.
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

    # Sync to Trello and Miro
    card_id = await trello_create_card(tid, description)
    if card_id:
        trello_map[tid] = card_id
        save_trello_map()

    miro_id = await miro_create_sticky(tid, description)
    if miro_id:
        miro_map[tid] = miro_id
        save_miro_map()

    sync_icons = []
    if card_id:
        sync_icons.append("🟦")
    if miro_id:
        sync_icons.append("🟡")
    sync_text = f" {' '.join(sync_icons)}" if sync_icons else ""

    await ctx.send(f"🆕 Task #{tid} created: *{description}*{sync_text}")


@bot.command()
async def task_done(ctx, tid: int):
    """Mark a task as completed (stays in table, moves to Done in Trello)."""
    if tid not in tasks_db:
        await ctx.send("❌ Task not found.")
        return
    await complete_task(
        ctx.channel, tid, tasks_db[tid]["assigned"] or ctx.author.display_name
    )


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
    await miro_update_sticky(tid, percent)
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


# ============================================================
#  Workflow Commands — powered by workflow_manager.py
# ============================================================


@bot.command()
async def workflow(ctx, *, description: str = ""):
    """Create a new AI workflow on Miro.
    Usage: !workflow client onboarding process
    """
    if not description:
        await ctx.send(
            "❌ Please provide a description.\n"
            "Example: `!workflow employee onboarding process`"
        )
        return
    async with ctx.typing():
        await workflow_manager.cmd_workflow_create(
            send_fn=ctx.send,
            description=description,
            author_name=ctx.author.display_name,
        )


@bot.command()
async def wf_edit(ctx, wf_id: str, *, edit_request: str):
    """Edit a workflow with natural language.
    Usage: !wf_edit 1 add a review step after approval
           !wf_edit 1 rename step 3 to Background Check
           !wf_edit 1 add a decision node after step 2
    """
    async with ctx.typing():
        await workflow_manager.cmd_workflow_edit(
            send_fn=ctx.send,
            wf_id=wf_id,
            edit_request=edit_request,
            author_name=ctx.author.display_name,
        )


@bot.command()
async def wf_undo(ctx, wf_id: str):
    """Undo the last edit on a workflow.
    Usage: !wf_undo 1
    """
    async with ctx.typing():
        await workflow_manager.cmd_workflow_undo(
            send_fn=ctx.send,
            wf_id=wf_id,
            author_name=ctx.author.display_name,
        )


@bot.command()
async def wf_list(ctx):
    """Show all saved workflows."""
    await workflow_manager.cmd_workflow_list(send_fn=ctx.send)


@bot.command()
async def wf_view(ctx, wf_id: str):
    """View steps of a workflow as text.
    Usage: !wf_view 1
    """
    await workflow_manager.cmd_workflow_view(send_fn=ctx.send, wf_id=wf_id)


@bot.command()
async def wf_delete(ctx, wf_id: str):
    """Delete a workflow from Discord + Miro.
    Usage: !wf_delete 1
    """
    await workflow_manager.cmd_workflow_delete(send_fn=ctx.send, wf_id=wf_id)


@bot.command()
async def wf_redraw(ctx, wf_id: str):
    """Force redraw a workflow on Miro (if someone messed with it manually).
    Usage: !wf_redraw 1
    """
    async with ctx.typing():
        await workflow_manager.cmd_workflow_redraw(send_fn=ctx.send, wf_id=wf_id)


# ==========================================================
#  Run
# ==========================================================

bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)
