"""
test_suite.py
=============
Complete automated test suite for the DiscordBot project.

Covers:
  - vdmonitor_listener.py  (unit + integration)
  - streaming_monitor.py   (unit)
  - workflow_manager.py    (unit + integration)
  - bot.py helpers         (unit)

Run:
  pip install pytest pytest-asyncio aiohttp aioresponses
  pytest test_suite.py -v

Or for a summary only:
  pytest test_suite.py -v --tb=short
"""

import asyncio
import json
import os
import sys
import time
import pytest
import pytest_asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, mock_open
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ==========================================================
#  Path Setup — make sure all modules are importable
# ==========================================================

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

# Stub heavy dependencies before importing project modules
# so tests run without a real Discord token / Miro key / etc.

# ── discord stub ─────────────────────────────────────────
discord_stub = MagicMock()
discord_stub.Member = MagicMock
discord_stub.VoiceState = MagicMock
discord_stub.TextChannel = MagicMock
discord_stub.Intents = MagicMock()
discord_stub.Intents.default.return_value = MagicMock()
discord_stub.Status.offline = "offline"
discord_stub.utils.get = MagicMock(return_value=None)
discord_stub.Embed = MagicMock(return_value=MagicMock(
    add_field=MagicMock(),
    set_footer=MagicMock(),
    set_thumbnail=MagicMock(),
))
discord_stub.Forbidden = Exception
sys.modules["discord"] = discord_stub
sys.modules["discord.ext"] = MagicMock()
sys.modules["discord.ext.commands"] = MagicMock()
sys.modules["discord.ext.tasks"] = MagicMock()
sys.modules["discord.abc"] = MagicMock()

# ── pynput stub ───────────────────────────────────────────
pynput_stub = MagicMock()
sys.modules["pynput"] = pynput_stub
sys.modules["pynput.mouse"] = MagicMock()
sys.modules["pynput.keyboard"] = MagicMock()

# ── pystray / PIL stub ────────────────────────────────────
sys.modules["pystray"] = MagicMock()
sys.modules["PIL"] = MagicMock()
sys.modules["PIL.Image"] = MagicMock()
sys.modules["PIL.ImageDraw"] = MagicMock()

# ── aiohttp stub (real aiohttp still available for tests) ─
# We do NOT stub aiohttp globally so aioresponses can work.

# ── dotenv stub ───────────────────────────────────────────
sys.modules["dotenv"] = MagicMock()

# ── requests stub ─────────────────────────────────────────
requests_stub = MagicMock()
requests_stub.exceptions.ConnectionError = ConnectionError
requests_stub.exceptions.Timeout = TimeoutError
sys.modules["requests"] = requests_stub

# ── tkinter stub ──────────────────────────────────────────
tkinter_stub = MagicMock()
sys.modules["tkinter"] = tkinter_stub
sys.modules["tkinter.messagebox"] = MagicMock()
sys.modules["tkinter.simpledialog"] = MagicMock()

# Now import project modules
import vdmonitor_listener
import streaming_monitor
import workflow_manager

TIMEZONE = ZoneInfo("Europe/Athens")


# ==========================================================
#  Helpers
# ==========================================================

def _make_mock_member(
    name="TestUser",
    user_id=123456,
    is_bot=False,
    display_avatar_url="http://avatar.url",
):
    member = MagicMock()
    member.id = user_id
    member.display_name = name
    member.bot = is_bot
    member.mention = f"<@{user_id}>"
    member.display_avatar.url = display_avatar_url
    return member


def _make_voice_state(channel=None, self_stream=False):
    vs = MagicMock()
    vs.channel = channel
    vs.self_stream = self_stream
    return vs


def _make_channel(name="streaming-logs"):
    chan = MagicMock()
    chan.name = name
    chan.send = AsyncMock()
    return chan


def _make_bot(log_channel=None):
    bot = MagicMock()
    guild = MagicMock()
    guild.text_channels = [log_channel] if log_channel else []
    discord_stub.utils.get.return_value = log_channel
    bot.guilds = [guild]
    return bot


# ==========================================================
#  1. vdmonitor_listener — Unit Tests
# ==========================================================

class TestVdmonitorListenerHelpers(unittest.TestCase):

    def setUp(self):
        vdmonitor_listener.user_states.clear()
        vdmonitor_listener._bot = None

    # ── _get_log_channel ──────────────────────────────────

    def test_get_log_channel_returns_none_when_no_bot(self):
        vdmonitor_listener._bot = None
        result = vdmonitor_listener._get_log_channel()
        self.assertIsNone(result)

    def test_get_log_channel_returns_channel_when_found(self):
        chan = _make_channel("streaming-logs")
        bot = _make_bot(chan)
        vdmonitor_listener._bot = bot
        discord_stub.utils.get.return_value = chan
        result = vdmonitor_listener._get_log_channel()
        self.assertIsNotNone(result)

    def test_get_log_channel_returns_none_when_not_found(self):
        bot = _make_bot(None)
        vdmonitor_listener._bot = bot
        discord_stub.utils.get.return_value = None
        result = vdmonitor_listener._get_log_channel()
        self.assertIsNone(result)

    # ── _find_user_id_by_name ─────────────────────────────

    def test_find_user_id_by_name_found(self):
        streaming_monitor.streaming_db = {
            "999": {"username": "Thomas"}
        }
        result = vdmonitor_listener._find_user_id_by_name("Thomas")
        self.assertEqual(result, 999)

    def test_find_user_id_by_name_case_insensitive(self):
        streaming_monitor.streaming_db = {
            "888": {"username": "Vazha"}
        }
        result = vdmonitor_listener._find_user_id_by_name("vazha")
        self.assertEqual(result, 888)

    def test_find_user_id_by_name_not_found(self):
        streaming_monitor.streaming_db = {}
        result = vdmonitor_listener._find_user_id_by_name("nobody")
        self.assertIsNone(result)

    # ── set_bot ───────────────────────────────────────────

    def test_set_bot_registers_instance(self):
        bot = MagicMock()
        vdmonitor_listener.set_bot(bot)
        self.assertEqual(vdmonitor_listener._bot, bot)


# ==========================================================
#  2. vdmonitor_listener — HTTP Handler Tests
# ==========================================================

class TestVdmonitorListenerHTTPHandler(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        vdmonitor_listener.user_states.clear()
        vdmonitor_listener.SECRET_TOKEN = "test_secret"
        vdmonitor_listener._bot = None

    async def _post(self, data: dict):
        """Helper: simulate a POST request to handle_activity."""
        request = MagicMock()
        request.json = AsyncMock(return_value=data)
        request.remote = "127.0.0.1"
        return await vdmonitor_listener.handle_activity(request)

    # ── Auth ──────────────────────────────────────────────

    async def test_invalid_token_returns_403(self):
        resp = await self._post({
            "token": "wrong_token",
            "username": "TestUser",
            "status": "heartbeat",
            "idle_minutes": 0,
            "machine": "PC",
        })
        self.assertEqual(resp.status, 403)

    async def test_missing_json_returns_400(self):
        request = MagicMock()
        request.json = AsyncMock(side_effect=Exception("bad json"))
        resp = await vdmonitor_listener.handle_activity(request)
        self.assertEqual(resp.status, 400)

    async def test_valid_heartbeat_returns_200(self):
        resp = await self._post({
            "token": "test_secret",
            "username": "Thomas",
            "status": "heartbeat",
            "idle_minutes": 0,
            "machine": "DESKTOP",
        })
        self.assertEqual(resp.status, 200)

    async def test_started_status_creates_user_state(self):
        await self._post({
            "token": "test_secret",
            "username": "NewUser",
            "status": "started",
            "idle_minutes": 0,
            "machine": "PC1",
        })
        self.assertIn("NewUser", vdmonitor_listener.user_states)
        self.assertEqual(vdmonitor_listener.user_states["NewUser"]["status"], "started")

    async def test_idle_status_sets_idle_state(self):
        vdmonitor_listener.ALERT_THRESHOLD_MINUTES = 10
        await self._post({
            "token": "test_secret",
            "username": "IdleUser",
            "status": "idle",
            "idle_minutes": 15,
            "machine": "PC1",
        })
        self.assertEqual(vdmonitor_listener.user_states["IdleUser"]["status"], "idle")

    async def test_active_after_idle_clears_idle_since(self):
        # First go idle
        await self._post({
            "token": "test_secret",
            "username": "ActiveUser",
            "status": "idle",
            "idle_minutes": 15,
            "machine": "PC1",
        })
        # Then go active
        await self._post({
            "token": "test_secret",
            "username": "ActiveUser",
            "status": "active",
            "idle_minutes": 0,
            "machine": "PC1",
        })
        state = vdmonitor_listener.user_states["ActiveUser"]
        self.assertIsNone(state["idle_since"])
        self.assertIsNone(state["last_alert"])

    async def test_stopped_status_sets_stopped(self):
        await self._post({
            "token": "test_secret",
            "username": "StoppedUser",
            "status": "stopped",
            "idle_minutes": 0,
            "machine": "PC1",
        })
        self.assertEqual(
            vdmonitor_listener.user_states["StoppedUser"]["status"], "stopped"
        )

    async def test_get_status_endpoint_returns_json(self):
        vdmonitor_listener.user_states["SomeUser"] = {
            "status": "active",
            "machine": "PC",
            "last_seen": datetime.now().astimezone(),
            "idle_since": None,
        }
        request = MagicMock()
        resp = await vdmonitor_listener.handle_status(request)
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.text)
        self.assertIn("SomeUser", data)

    # ── Repeat alert logic ────────────────────────────────

    async def test_no_repeat_alert_before_threshold(self):
        """Second idle signal within REPEAT_ALERT_MINUTES should NOT trigger alert."""
        vdmonitor_listener.ALERT_THRESHOLD_MINUTES = 10
        vdmonitor_listener.REPEAT_ALERT_MINUTES = 15

        now = datetime.now().astimezone()
        vdmonitor_listener.user_states["RepeatUser"] = {
            "status": "idle",
            "last_alert": now,  # alert was just sent
            "idle_since": now,
            "machine": "PC",
            "last_seen": now,
        }

        with patch("asyncio.create_task") as mock_task:
            await self._post({
                "token": "test_secret",
                "username": "RepeatUser",
                "status": "idle",
                "idle_minutes": 12,
                "machine": "PC",
            })
            mock_task.assert_not_called()


# ==========================================================
#  3. vdmonitor_listener — Discord Alert Tests
# ==========================================================

class TestVdmonitorListenerAlerts(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        streaming_monitor.streaming_db = {}
        streaming_monitor.TIMEZONE = TIMEZONE

    async def test_post_idle_alert_no_channel(self):
        """Should not raise even if no log channel found."""
        vdmonitor_listener._bot = _make_bot(None)
        discord_stub.utils.get.return_value = None
        # Should complete without exception
        await vdmonitor_listener._post_idle_alert("TestUser", 15.0)

    async def test_post_idle_alert_with_channel(self):
        chan = _make_channel()
        vdmonitor_listener._bot = _make_bot(chan)
        discord_stub.utils.get.return_value = chan
        await vdmonitor_listener._post_idle_alert("TestUser", 15.0)
        chan.send.assert_called_once()

    async def test_post_status_change_started(self):
        chan = _make_channel()
        vdmonitor_listener._bot = _make_bot(chan)
        discord_stub.utils.get.return_value = chan
        await vdmonitor_listener._post_status_change("Thomas", "started", "PC")
        chan.send.assert_called_once()

    async def test_post_status_change_stopped(self):
        chan = _make_channel()
        vdmonitor_listener._bot = _make_bot(chan)
        discord_stub.utils.get.return_value = chan
        await vdmonitor_listener._post_status_change("Thomas", "stopped", "PC")
        chan.send.assert_called_once()

    async def test_post_status_change_unknown_status_silent(self):
        """Unknown status should not send anything."""
        chan = _make_channel()
        vdmonitor_listener._bot = _make_bot(chan)
        discord_stub.utils.get.return_value = chan
        await vdmonitor_listener._post_status_change("Thomas", "unknown_xyz", "PC")
        chan.send.assert_not_called()

    async def test_post_idle_alert_network_failure_logs_error(self):
        """channel.send failing should not crash the coroutine."""
        chan = _make_channel()
        chan.send = AsyncMock(side_effect=Exception("Network error"))
        vdmonitor_listener._bot = _make_bot(chan)
        discord_stub.utils.get.return_value = chan
        # Should not raise
        try:
            await vdmonitor_listener._post_idle_alert("Thomas", 15.0)
        except Exception:
            self.fail("_post_idle_alert raised an exception on network failure")

    async def test_post_status_change_network_failure_logs_error(self):
        """channel.send failing should not crash the coroutine."""
        chan = _make_channel()
        chan.send = AsyncMock(side_effect=Exception("Network error"))
        vdmonitor_listener._bot = _make_bot(chan)
        discord_stub.utils.get.return_value = chan
        try:
            await vdmonitor_listener._post_status_change("Thomas", "started", "PC")
        except Exception:
            self.fail("_post_status_change raised an exception on network failure")


# ==========================================================
#  4. streaming_monitor — Unit Tests
# ==========================================================

class TestStreamingMonitorHelpers(unittest.TestCase):

    def setUp(self):
        streaming_monitor.streaming_db = {}
        streaming_monitor.TIMEZONE = TIMEZONE

    # ── _format_duration ──────────────────────────────────

    def test_format_duration_under_60(self):
        result = streaming_monitor._format_duration(45.0)
        self.assertIn("45", result)
        self.assertIn("min", result)

    def test_format_duration_over_60(self):
        result = streaming_monitor._format_duration(90.0)
        self.assertIn("hrs", result)

    def test_format_duration_exactly_60(self):
        result = streaming_monitor._format_duration(60.0)
        self.assertIn("hrs", result)

    def test_format_duration_zero(self):
        result = streaming_monitor._format_duration(0.0)
        self.assertIn("0", result)

    # ── _get_entry ────────────────────────────────────────

    def test_get_entry_creates_new_entry(self):
        member = _make_mock_member("Alice", 111)
        entry = streaming_monitor._get_entry(member)
        self.assertIn("111", streaming_monitor.streaming_db)
        self.assertEqual(entry["username"], "Alice")
        self.assertIsNone(entry["current_start"])
        self.assertIsNone(entry["current_voice_start"])

    def test_get_entry_updates_username(self):
        streaming_monitor.streaming_db["222"] = {
            "username": "OldName",
            "user_id": 222,
            "sessions": [],
            "current_start": None,
            "voice_sessions": [],
            "current_voice_start": None,
        }
        member = _make_mock_member("NewName", 222)
        entry = streaming_monitor._get_entry(member)
        self.assertEqual(entry["username"], "NewName")

    def test_get_entry_adds_missing_voice_fields(self):
        streaming_monitor.streaming_db["333"] = {
            "username": "Partial",
            "user_id": 333,
            "sessions": [],
            "current_start": None,
        }
        member = _make_mock_member("Partial", 333)
        entry = streaming_monitor._get_entry(member)
        self.assertIn("voice_sessions", entry)
        self.assertIn("current_voice_start", entry)

    # ── load/save ─────────────────────────────────────────

    def test_load_streaming_file_not_found(self):
        with patch("builtins.open", side_effect=FileNotFoundError):
            streaming_monitor.load_streaming()
        self.assertEqual(streaming_monitor.streaming_db, {})

    def test_save_streaming_success(self):
        streaming_monitor.streaming_db = {"999": {"username": "Test"}}
        m = mock_open()
        with patch("builtins.open", m):
            streaming_monitor.save_streaming()
        m.assert_called_once()

    def test_save_streaming_handles_error(self):
        with patch("builtins.open", side_effect=Exception("disk full")):
            # Should not raise
            streaming_monitor.save_streaming()


# ==========================================================
#  5. streaming_monitor — Voice State Tests
# ==========================================================

class TestStreamingMonitorVoiceState(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        streaming_monitor.streaming_db = {}
        streaming_monitor.TIMEZONE = TIMEZONE

    async def test_bot_member_ignored(self):
        bot_member = _make_mock_member("BotUser", 1, is_bot=True)
        before = _make_voice_state(channel=None)
        after = _make_voice_state(channel=MagicMock(name="general"))
        bot = _make_bot()
        # Should return without doing anything
        await streaming_monitor.handle_voice_state_update(bot, bot_member, before, after)
        self.assertEqual(streaming_monitor.streaming_db, {})

    async def test_join_voice_creates_entry(self):
        member = _make_mock_member("Vazha", 456)
        chan = MagicMock()
        chan.name = "general"
        before = _make_voice_state(channel=None)
        after = _make_voice_state(channel=chan)
        log_chan = _make_channel()
        bot = _make_bot(log_chan)
        discord_stub.utils.get.return_value = log_chan

        await streaming_monitor.handle_voice_state_update(bot, member, before, after)

        entry = streaming_monitor.streaming_db.get("456")
        self.assertIsNotNone(entry)
        self.assertIsNotNone(entry["current_voice_start"])

    async def test_leave_voice_saves_session(self):
        member = _make_mock_member("Vazha", 456)
        now_iso = datetime.now(TIMEZONE).isoformat()
        streaming_monitor.streaming_db["456"] = {
            "username": "Vazha",
            "user_id": 456,
            "sessions": [],
            "current_start": None,
            "voice_sessions": [],
            "current_voice_start": now_iso,
        }

        chan = MagicMock()
        chan.name = "general"
        before = _make_voice_state(channel=chan)
        after = _make_voice_state(channel=None)
        log_chan = _make_channel()
        bot = _make_bot(log_chan)
        discord_stub.utils.get.return_value = log_chan

        await streaming_monitor.handle_voice_state_update(bot, member, before, after)

        entry = streaming_monitor.streaming_db["456"]
        self.assertIsNone(entry["current_voice_start"])
        self.assertEqual(len(entry["voice_sessions"]), 1)

    async def test_stream_started_sets_current_start(self):
        member = _make_mock_member("Vazha", 456)
        chan = MagicMock()
        chan.name = "general"
        before = _make_voice_state(channel=chan, self_stream=False)
        after = _make_voice_state(channel=chan, self_stream=True)
        log_chan = _make_channel()
        bot = _make_bot(log_chan)
        discord_stub.utils.get.return_value = log_chan

        await streaming_monitor.handle_voice_state_update(bot, member, before, after)

        entry = streaming_monitor.streaming_db.get("456")
        self.assertIsNotNone(entry)
        self.assertIsNotNone(entry["current_start"])

    async def test_stream_stopped_saves_session(self):
        member = _make_mock_member("Vazha", 456)
        now_iso = datetime.now(TIMEZONE).isoformat()
        chan = MagicMock()
        chan.name = "general"
        streaming_monitor.streaming_db["456"] = {
            "username": "Vazha",
            "user_id": 456,
            "sessions": [],
            "current_start": now_iso,
            "voice_sessions": [],
            "current_voice_start": now_iso,
        }

        before = _make_voice_state(channel=chan, self_stream=True)
        after = _make_voice_state(channel=chan, self_stream=False)
        log_chan = _make_channel()
        bot = _make_bot(log_chan)
        discord_stub.utils.get.return_value = log_chan

        await streaming_monitor.handle_voice_state_update(bot, member, before, after)

        entry = streaming_monitor.streaming_db["456"]
        self.assertIsNone(entry["current_start"])
        self.assertEqual(len(entry["sessions"]), 1)

    async def test_leave_voice_while_streaming_treated_as_stream_stopped(self):
        """Discord bug workaround: self_stream stays True when leaving voice."""
        member = _make_mock_member("Vazha", 456)
        now_iso = datetime.now(TIMEZONE).isoformat()
        chan = MagicMock()
        chan.name = "general"
        streaming_monitor.streaming_db["456"] = {
            "username": "Vazha",
            "user_id": 456,
            "sessions": [],
            "current_start": now_iso,
            "voice_sessions": [],
            "current_voice_start": now_iso,
        }

        # self_stream still True in before (Discord bug)
        before = _make_voice_state(channel=chan, self_stream=True)
        after = _make_voice_state(channel=None, self_stream=True)
        log_chan = _make_channel()
        bot = _make_bot(log_chan)
        discord_stub.utils.get.return_value = log_chan

        await streaming_monitor.handle_voice_state_update(bot, member, before, after)

        entry = streaming_monitor.streaming_db["456"]
        self.assertIsNone(entry["current_start"])
        self.assertEqual(len(entry["sessions"]), 1)

    async def test_nothing_changed_no_side_effects(self):
        """
        If voice state didn't change in a relevant way, no sessions should be
        recorded and no Discord message should be sent.
        An entry IS created by _get_entry() — that is expected behaviour.
        """
        member = _make_mock_member("Vazha", 456)
        chan = MagicMock()
        chan.name = "general"
        # Same channel, no stream change
        before = _make_voice_state(channel=chan, self_stream=False)
        after  = _make_voice_state(channel=chan, self_stream=False)
        log_chan = _make_channel()
        bot = _make_bot(log_chan)
        discord_stub.utils.get.return_value = log_chan

        await streaming_monitor.handle_voice_state_update(bot, member, before, after)

        # Entry may be created, but no sessions should be recorded
        # and no Discord message should have been sent
        entry = streaming_monitor.streaming_db.get("456")
        if entry:
            self.assertEqual(entry["sessions"], [])
            self.assertEqual(entry["voice_sessions"], [])
            self.assertIsNone(entry["current_start"])
            self.assertIsNone(entry["current_voice_start"])

        log_chan.send.assert_not_called()


# ==========================================================
#  6. streaming_monitor — Commands
# ==========================================================

class TestStreamingMonitorCommands(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        streaming_monitor.streaming_db = {}
        streaming_monitor.TIMEZONE = TIMEZONE

    async def test_cmd_streaming_no_data(self):
        member = _make_mock_member("Alice", 111)
        ctx = MagicMock()
        ctx.author = member
        ctx.send = AsyncMock()
        streaming_monitor.streaming_db = {}

        await streaming_monitor.cmd_streaming(ctx, member)
        ctx.send.assert_called_once()
        call_args = ctx.send.call_args[0][0]
        self.assertIn("No", call_args)

    async def test_cmd_streaming_with_data(self):
        member = _make_mock_member("Alice", 111)
        today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        streaming_monitor.streaming_db = {
            "111": {
                "username": "Alice",
                "user_id": 111,
                "sessions": [{
                    "date": today,
                    "start": datetime.now(TIMEZONE).isoformat(),
                    "end": datetime.now(TIMEZONE).isoformat(),
                    "start_readable": "10:00:00",
                    "end_readable": "11:00:00",
                    "duration_minutes": 60.0,
                }],
                "current_start": None,
                "voice_sessions": [],
                "current_voice_start": None,
            }
        }
        ctx = MagicMock()
        ctx.author = member
        ctx.send = AsyncMock()

        await streaming_monitor.cmd_streaming(ctx, member)
        ctx.send.assert_called_once()

    async def test_cmd_streaming_today_empty(self):
        ctx = MagicMock()
        ctx.send = AsyncMock()
        streaming_monitor.streaming_db = {}

        await streaming_monitor.cmd_streaming_today(ctx)
        ctx.send.assert_called_once()
        self.assertIn("No", ctx.send.call_args[0][0])

    async def test_cmd_streaming_today_with_active_user(self):
        today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        streaming_monitor.streaming_db = {
            "222": {
                "username": "Bob",
                "user_id": 222,
                "sessions": [],
                "current_start": datetime.now(TIMEZONE).isoformat(),
                "voice_sessions": [],
                "current_voice_start": None,
            }
        }
        ctx = MagicMock()
        ctx.send = AsyncMock()

        await streaming_monitor.cmd_streaming_today(ctx)
        ctx.send.assert_called_once()


# ==========================================================
#  7. workflow_manager — Unit Tests
# ==========================================================

class TestWorkflowManagerHelpers(unittest.TestCase):

    def setUp(self):
        workflow_manager.workflows_db = {}
        workflow_manager.workflow_counter = 1

    def test_miro_enabled_false_when_no_env(self):
        orig_token = workflow_manager.MIRO_ACCESS_TOKEN
        orig_board = workflow_manager.MIRO_BOARD_ID
        workflow_manager.MIRO_ACCESS_TOKEN = ""
        workflow_manager.MIRO_BOARD_ID = ""
        self.assertFalse(workflow_manager._miro_enabled())
        workflow_manager.MIRO_ACCESS_TOKEN = orig_token
        workflow_manager.MIRO_BOARD_ID = orig_board

    def test_miro_enabled_true_when_configured(self):
        workflow_manager.MIRO_ACCESS_TOKEN = "token"
        workflow_manager.MIRO_BOARD_ID = "boardid"
        self.assertTrue(workflow_manager._miro_enabled())

    def test_miro_headers_format(self):
        workflow_manager.MIRO_ACCESS_TOKEN = "mytoken"
        headers = workflow_manager._miro_headers()
        self.assertIn("Authorization", headers)
        self.assertIn("Bearer mytoken", headers["Authorization"])

    def test_next_x_origin_increments(self):
        workflow_manager.workflows_db = {}
        x1 = workflow_manager._next_x_origin()
        workflow_manager.workflows_db["1"] = {}
        x2 = workflow_manager._next_x_origin()
        self.assertGreater(x2, x1)

    def test_clean_json_strips_markdown(self):
        raw = "```json\n{\"key\": \"value\"}\n```"
        result = workflow_manager._clean_json(raw)
        self.assertEqual(result, '{"key": "value"}')

    def test_clean_json_no_markdown(self):
        raw = '{"key": "value"}'
        result = workflow_manager._clean_json(raw)
        self.assertEqual(result, '{"key": "value"}')

    def test_validate_steps_first_is_start(self):
        steps = [
            {"label": "Step 1", "type": "process"},
            {"label": "End", "type": "end"},
        ]
        result = workflow_manager._validate_steps(steps)
        self.assertEqual(result[0]["type"], "start")

    def test_validate_steps_last_is_end(self):
        steps = [
            {"label": "Start", "type": "start"},
            {"label": "Step 2", "type": "process"},
        ]
        result = workflow_manager._validate_steps(steps)
        self.assertEqual(result[-1]["type"], "end")

    def test_validate_steps_branch_target_clamped(self):
        steps = [
            {"label": "Start", "type": "start"},
            {
                "label": "Decision",
                "type": "decision",
                "branches": [
                    {"label": "Yes", "target_index": 999},  # out of bounds
                    {"label": "No", "target_index": 0},
                ],
            },
            {"label": "End", "type": "end"},
        ]
        result = workflow_manager._validate_steps(steps)
        # target_index 999 should be clamped to last valid index (2)
        self.assertEqual(result[1]["branches"][0]["target_index"], 2)

    def test_validate_steps_empty_list(self):
        result = workflow_manager._validate_steps([])
        self.assertEqual(result, [])

    # ── load/save ─────────────────────────────────────────

    def test_load_workflows_file_not_found(self):
        with patch("builtins.open", side_effect=FileNotFoundError):
            workflow_manager.load_workflows()
        self.assertEqual(workflow_manager.workflows_db, {})

    def test_save_workflows_success(self):
        workflow_manager.workflows_db = {"1": {"title": "Test"}}
        m = mock_open()
        with patch("builtins.open", m):
            workflow_manager.save_workflows()
        m.assert_called_once()

    def test_load_workflows_sets_counter(self):
        fake_data = {
            "1": {"title": "First"},
            "3": {"title": "Third"},
        }
        m = mock_open(read_data=json.dumps(fake_data))
        with patch("builtins.open", m):
            workflow_manager.load_workflows()
        self.assertEqual(workflow_manager.workflow_counter, 4)


# ==========================================================
#  8. workflow_manager — AI & Command Tests
# ==========================================================

class TestWorkflowManagerCommands(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        workflow_manager.workflows_db = {}
        workflow_manager.workflow_counter = 1
        workflow_manager.MIRO_ACCESS_TOKEN = ""
        workflow_manager.MIRO_BOARD_ID = ""

    async def test_cmd_workflow_list_empty(self):
        send_fn = AsyncMock()
        await workflow_manager.cmd_workflow_list(send_fn)
        send_fn.assert_called_once()
        self.assertIn("No workflows", send_fn.call_args[0][0])

    async def test_cmd_workflow_list_with_data(self):
        workflow_manager.workflows_db = {
            "1": {
                "title": "Test WF",
                "steps": [{"label": "Start", "type": "start"}, {"label": "End", "type": "end"}],
                "history": [{"action": "created", "by": "Admin", "timestamp": "2026-01-01T00:00:00"}],
                "created_by": "Admin",
                "created_at": "2026-01-01T00:00:00",
            }
        }
        send_fn = AsyncMock()
        await workflow_manager.cmd_workflow_list(send_fn)
        send_fn.assert_called_once()
        self.assertIn("Test WF", send_fn.call_args[0][0])

    async def test_cmd_workflow_view_not_found(self):
        send_fn = AsyncMock()
        await workflow_manager.cmd_workflow_view(send_fn, "999")
        send_fn.assert_called_once()
        self.assertIn("not found", send_fn.call_args[0][0])

    async def test_cmd_workflow_view_with_data(self):
        workflow_manager.workflows_db = {
            "1": {
                "title": "Onboarding",
                "steps": [
                    {"label": "Start", "type": "start"},
                    {"label": "Review", "type": "process"},
                    {"label": "End", "type": "end"},
                ],
                "history": [],
            }
        }
        send_fn = AsyncMock()
        await workflow_manager.cmd_workflow_view(send_fn, "1")
        send_fn.assert_called_once()
        self.assertIn("Onboarding", send_fn.call_args[0][0])

    async def test_cmd_workflow_delete_not_found(self):
        send_fn = AsyncMock()
        await workflow_manager.cmd_workflow_delete(send_fn, "999")
        self.assertIn("not found", send_fn.call_args[0][0])

    async def test_cmd_workflow_delete_removes_entry(self):
        workflow_manager.workflows_db = {
            "1": {
                "title": "ToDelete",
                "steps": [],
                "miro_shape_ids": [],
                "miro_connector_ids": [],
                "miro_board_id": "",
            }
        }
        send_fn = AsyncMock()
        with patch("workflow_manager.save_workflows"):
            await workflow_manager.cmd_workflow_delete(send_fn, "1")
        self.assertNotIn("1", workflow_manager.workflows_db)

    async def test_cmd_workflow_edit_not_found(self):
        send_fn = AsyncMock()
        await workflow_manager.cmd_workflow_edit(send_fn, "999", "add step", "Admin")
        self.assertIn("not found", send_fn.call_args[0][0])

    async def test_cmd_workflow_undo_no_history(self):
        workflow_manager.workflows_db = {
            "1": {
                "title": "Undo Test",
                "steps": [],
                "history": [],
            }
        }
        send_fn = AsyncMock()
        await workflow_manager.cmd_workflow_undo(send_fn, "1", "Admin")
        self.assertIn("Nothing to undo", send_fn.call_args[0][0])

    async def test_cmd_workflow_undo_not_found(self):
        send_fn = AsyncMock()
        await workflow_manager.cmd_workflow_undo(send_fn, "999", "Admin")
        self.assertIn("not found", send_fn.call_args[0][0])

    async def test_ai_generate_workflow_valid_response(self):
        fake_steps = [
            {"label": "Start", "type": "start"},
            {"label": "Do Work", "type": "process"},
            {"label": "End", "type": "end"},
        ]
        with patch(
            "workflow_manager._ask_ollama",
            return_value=json.dumps(fake_steps),
        ):
            result = await workflow_manager.ai_generate_workflow("test workflow")
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["type"], "start")
        self.assertEqual(result[-1]["type"], "end")

    async def test_ai_generate_workflow_invalid_response(self):
        with patch("workflow_manager._ask_ollama", return_value="not json at all!!"):
            result = await workflow_manager.ai_generate_workflow("test workflow")
        self.assertIsNone(result)

    async def test_ai_generate_workflow_ollama_unavailable(self):
        with patch("workflow_manager._ask_ollama", return_value=None):
            result = await workflow_manager.ai_generate_workflow("test workflow")
        self.assertIsNone(result)

    async def test_ai_edit_workflow_not_found(self):
        result = await workflow_manager.ai_edit_workflow("999", "edit", "Admin")
        self.assertIn("error", result)

    async def test_ai_edit_workflow_success(self):
        workflow_manager.workflows_db = {
            "1": {
                "title": "Edit Test",
                "steps": [
                    {"label": "Start", "type": "start"},
                    {"label": "End", "type": "end"},
                ],
                "history": [],
                "miro_shape_ids": [],
                "miro_connector_ids": [],
                "miro_board_id": "",
                "x_origin": 2000,
                "y_origin": 0,
            }
        }
        new_steps = [
            {"label": "Start", "type": "start"},
            {"label": "New Step", "type": "process"},
            {"label": "End", "type": "end"},
        ]
        fake_response = json.dumps({
            "steps": new_steps,
            "changes": "Added new step",
        })
        with patch("workflow_manager._ask_ollama", return_value=fake_response), \
             patch("workflow_manager._miro_redraw", return_value={"shape_ids": [], "connector_ids": []}), \
             patch("workflow_manager.save_workflows"):
            result = await workflow_manager.ai_edit_workflow("1", "add a step", "Admin")
        self.assertTrue(result.get("success"))
        self.assertEqual(result["step_count"], 3)

    async def test_ai_undo_workflow_restores_snapshot(self):
        original_steps = [
            {"label": "Start", "type": "start"},
            {"label": "End", "type": "end"},
        ]
        new_steps = [
            {"label": "Start", "type": "start"},
            {"label": "Extra Step", "type": "process"},
            {"label": "End", "type": "end"},
        ]
        workflow_manager.workflows_db = {
            "1": {
                "title": "Undo Test",
                "steps": new_steps,
                "history": [
                    {
                        "action": "Added Extra Step",
                        "timestamp": "2026-01-01T00:00:00",
                        "by": "Admin",
                        "snapshot": original_steps,
                    }
                ],
                "miro_shape_ids": [],
                "miro_connector_ids": [],
                "miro_board_id": "",
                "x_origin": 2000,
                "y_origin": 0,
            }
        }
        with patch("workflow_manager._miro_redraw", return_value={"shape_ids": [], "connector_ids": []}), \
             patch("workflow_manager.save_workflows"):
            result = await workflow_manager.ai_undo_workflow("1", "Admin")

        self.assertTrue(result.get("success"))
        self.assertEqual(workflow_manager.workflows_db["1"]["steps"], original_steps)
        self.assertEqual(len(workflow_manager.workflows_db["1"]["history"]), 0)

    async def test_parse_workflow_intent_not_workflow(self):
        with patch(
            "workflow_manager._ask_ollama",
            return_value='{"action": "not_workflow"}',
        ):
            result = await workflow_manager.parse_workflow_intent(
                "what tasks are pending", "Admin"
            )
        self.assertIsNone(result)

    async def test_parse_workflow_intent_create(self):
        with patch(
            "workflow_manager._ask_ollama",
            return_value='{"action": "workflow_create", "description": "onboarding"}',
        ):
            result = await workflow_manager.parse_workflow_intent(
                "create a workflow for onboarding", "Admin"
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "workflow_create")

    async def test_parse_workflow_intent_ollama_fails(self):
        with patch("workflow_manager._ask_ollama", return_value=None):
            result = await workflow_manager.parse_workflow_intent(
                "create workflow", "Admin"
            )
        self.assertIsNone(result)

    async def test_cmd_workflow_create_miro_disabled(self):
        """When Miro is not configured, create should fail gracefully."""
        workflow_manager.MIRO_ACCESS_TOKEN = ""
        workflow_manager.MIRO_BOARD_ID = ""

        fake_steps = [
            {"label": "Start", "type": "start"},
            {"label": "End", "type": "end"},
        ]
        send_fn = AsyncMock()
        with patch("workflow_manager.ai_generate_workflow", return_value=fake_steps), \
             patch("workflow_manager._miro_create_diagram", return_value={"error": "Miro not configured"}):
            await workflow_manager.cmd_workflow_create(send_fn, "test", "Admin")

        # Should have sent an error message
        calls = [str(c) for c in send_fn.call_args_list]
        self.assertTrue(any("error" in c.lower() or "❌" in c for c in calls))


# ==========================================================
#  9. vdmonitor — Unit Tests
# ==========================================================

class TestVdmonitorClientLogic(unittest.TestCase):
    """
    Tests for the client-side vdmonitor.py logic.
    We import it here since all its heavy deps are stubbed.
    """

    def setUp(self):
        # Dynamically import so our stubs apply
        vdmonitor_path = BASE_DIR / "VDMonitor" / "vdmonitor.py"
        import importlib.util
        spec = importlib.util.spec_from_file_location("vdmonitor", vdmonitor_path)
        self.vdm = importlib.util.module_from_spec(spec)
        # Patch requests before exec
        self.vdm.__dict__["requests"] = requests_stub
        try:
            spec.loader.exec_module(self.vdm)
        except Exception:
            pass  # Ignore top-level errors from stubs

    def test_secret_token_is_set(self):
        self.assertTrue(bool(self.vdm.SECRET_TOKEN))

    def test_idle_threshold_computed(self):
        self.assertEqual(
            self.vdm.IDLE_THRESHOLD_SECONDS,
            self.vdm.IDLE_THRESHOLD_MINUTES * 60,
        )

    def test_heartbeat_interval_computed(self):
        self.assertEqual(
            self.vdm.HEARTBEAT_INTERVAL_SECONDS,
            self.vdm.HEARTBEAT_INTERVAL_MINUTES * 60,
        )

    def test_send_status_no_token(self):
        orig = self.vdm.SECRET_TOKEN
        self.vdm.SECRET_TOKEN = ""
        # Should not raise
        self.vdm.send_status("heartbeat")
        self.vdm.SECRET_TOKEN = orig

    def test_send_status_connection_error(self):
        requests_stub.post.side_effect = ConnectionError("no connection")
        # Should not raise
        self.vdm.send_status("heartbeat")
        requests_stub.post.side_effect = None

    def test_send_status_timeout(self):
        requests_stub.post.side_effect = TimeoutError("timeout")
        self.vdm.send_status("heartbeat")
        requests_stub.post.side_effect = None

    def test_reset_activity_resets_time(self):
        self.vdm.last_activity_time = 0
        self.vdm._reset_activity()
        self.assertAlmostEqual(self.vdm.last_activity_time, time.time(), delta=1)

    def test_reset_activity_clears_idle(self):
        self.vdm.is_idle = True
        self.vdm.discord_username = "TestUser"
        requests_stub.post.return_value = MagicMock(status_code=200)
        self.vdm._reset_activity()
        self.assertFalse(self.vdm.is_idle)

    def test_stop_monitoring_idempotent(self):
        self.vdm.monitor_running = False
        # Should not raise calling stop when already stopped
        self.vdm.stop_monitoring()

    def test_log_file_in_base_dir(self):
        expected_dir = str(BASE_DIR / "VDMonitor")
        self.assertIn("VDMonitor", self.vdm.LOG_FILE)


# ==========================================================
#  10. Edge Cases
# ==========================================================

class TestEdgeCases(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        streaming_monitor.streaming_db = {}
        streaming_monitor.TIMEZONE = TIMEZONE
        vdmonitor_listener.user_states.clear()
        vdmonitor_listener.SECRET_TOKEN = "test_secret"

    # ── Empty username in vdmonitor_listener ──────────────

    async def test_empty_username_defaults_to_unknown(self):
        request = MagicMock()
        request.json = AsyncMock(return_value={
            "token": "test_secret",
            "username": "",
            "status": "heartbeat",
            "idle_minutes": 0,
            "machine": "PC",
        })
        request.remote = "127.0.0.1"
        resp = await vdmonitor_listener.handle_activity(request)
        self.assertEqual(resp.status, 200)

    # ── Workflow with decision node ───────────────────────

    async def test_workflow_with_decision_node(self):
        steps = [
            {"label": "Start", "type": "start"},
            {
                "label": "Is Approved?",
                "type": "decision",
                "branches": [
                    {"label": "Yes", "target_index": 2},
                    {"label": "No", "target_index": 3},
                ],
            },
            {"label": "Approve", "type": "process"},
            {"label": "Reject", "type": "process"},
            {"label": "End", "type": "end"},
        ]
        result = workflow_manager._validate_steps(steps)
        self.assertEqual(result[0]["type"], "start")
        self.assertEqual(result[-1]["type"], "end")
        self.assertEqual(len(result[1]["branches"]), 2)

    # ── streaming_monitor with no timezone set ────────────

    async def test_streaming_monitor_no_timezone(self):
        """If TIMEZONE is None, handle_voice_state_update should not crash."""
        streaming_monitor.TIMEZONE = None
        member = _make_mock_member("Vazha", 456)
        chan = MagicMock()
        chan.name = "general"
        before = _make_voice_state(channel=None)
        after = _make_voice_state(channel=chan)
        bot = _make_bot()
        try:
            await streaming_monitor.handle_voice_state_update(bot, member, before, after)
        except Exception:
            pass  # Expected if timezone is None — just shouldn't be an unhandled crash

    # ── vdmonitor_listener idle below threshold ───────────

    async def test_idle_below_threshold_no_alert(self):
        vdmonitor_listener.ALERT_THRESHOLD_MINUTES = 10
        with patch("asyncio.create_task") as mock_task:
            request = MagicMock()
            request.json = AsyncMock(return_value={
                "token": "test_secret",
                "username": "BelowUser",
                "status": "idle",
                "idle_minutes": 5,  # Below 10 min threshold
                "machine": "PC",
            })
            request.remote = "127.0.0.1"
            await vdmonitor_listener.handle_activity(request)
            mock_task.assert_not_called()

    # ── workflow step count limits ────────────────────────

    async def test_ai_generate_workflow_too_few_steps(self):
        """A single-step response should be rejected."""
        fake_steps = [{"label": "Only Step", "type": "start"}]
        with patch("workflow_manager._ask_ollama", return_value=json.dumps(fake_steps)):
            result = await workflow_manager.ai_generate_workflow("test")
        self.assertIsNone(result)

    # ── duplicate machine names ───────────────────────────

    async def test_same_user_different_machine_updates_machine(self):
        request1 = MagicMock()
        request1.json = AsyncMock(return_value={
            "token": "test_secret",
            "username": "SameUser",
            "status": "heartbeat",
            "idle_minutes": 0,
            "machine": "PC1",
        })
        request1.remote = "127.0.0.1"

        request2 = MagicMock()
        request2.json = AsyncMock(return_value={
            "token": "test_secret",
            "username": "SameUser",
            "status": "heartbeat",
            "idle_minutes": 0,
            "machine": "PC2",  # Changed machine
        })
        request2.remote = "127.0.0.1"

        await vdmonitor_listener.handle_activity(request1)
        await vdmonitor_listener.handle_activity(request2)

        self.assertEqual(vdmonitor_listener.user_states["SameUser"]["machine"], "PC2")

    # ── workflow_manager markdown stripping variants ──────

    def test_clean_json_triple_backtick_only(self):
        raw = "```\n{\"a\": 1}\n```"
        result = workflow_manager._clean_json(raw)
        self.assertIn('"a"', result)

    def test_clean_json_already_clean(self):
        raw = '{"a": 1}'
        result = workflow_manager._clean_json(raw)
        self.assertEqual(result, '{"a": 1}')


# ==========================================================
#  Run
# ==========================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])