"""
vdmonitor.py
A lightweight background monitor that detects keyboard/mouse inactivity
and reports it to your Discord bot server.

Requirements:
    pip install pynput requests pystray pillow

To build as .exe:
    pip install pyinstaller
    pyinstaller --onefile --windowed --name "VDMonitor" vdmonitor.py
"""

import time
import threading
import requests
import os
import sys
import logging
from datetime import datetime
from pathlib import Path

from pynput import mouse, keyboard

# ── Optional system tray support ────────────────────────────────────────────
try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False


# ==========================================================
#  Load Config from vdmonitor_config.env
# ==========================================================

def load_config() -> dict:
    """
    Load config from vdmonitor_config.env sitting next to the .exe / .py.
    Falls back to environment variables, then to safe defaults.
    """
    defaults = {
        "BOT_SERVER_URL":              "http://192.168.10.46:8765/activity",
        "SECRET_TOKEN":                "",
        "DISCORD_USERNAME":            "",
        "IDLE_THRESHOLD_MINUTES":      "10",
        "CHECK_INTERVAL_SECONDS":      "30",
        "HEARTBEAT_INTERVAL_MINUTES":  "2",
    }

    # Locate the directory that contains the running file
    if getattr(sys, "frozen", False):
        # Running as a PyInstaller .exe
        base_dir = Path(sys.executable).parent
    else:
        base_dir = Path(__file__).parent

    env_file = base_dir / "vdmonitor_config.env"

    config = dict(defaults)

    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip()
                    if key in config:
                        config[key] = value
        print(f"[CONFIG] Loaded config from {env_file}")
    else:
        print(f"[CONFIG] ⚠️  No config file found at {env_file}")
        print(f"[CONFIG]     Create 'vdmonitor_config.env' next to this file.")

    # Environment variables take highest priority
    for key in config:
        env_val = os.environ.get(key)
        if env_val:
            config[key] = env_val

    return config


CONFIG = load_config()

BOT_SERVER_URL            = CONFIG["BOT_SERVER_URL"]
SECRET_TOKEN              = CONFIG["SECRET_TOKEN"]
DISCORD_USERNAME          = CONFIG["DISCORD_USERNAME"]
IDLE_THRESHOLD_SECONDS    = int(CONFIG["IDLE_THRESHOLD_MINUTES"]) * 60
CHECK_INTERVAL_SECONDS    = int(CONFIG["CHECK_INTERVAL_SECONDS"])
HEARTBEAT_INTERVAL_SECONDS = int(CONFIG["HEARTBEAT_INTERVAL_MINUTES"]) * 60

LOG_FILE = os.path.join(os.path.expanduser("~"), "vdmonitor.log")


# ==========================================================
#  Logging
# ==========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("vdmonitor")


# ==========================================================
#  State
# ==========================================================

last_activity_time = time.time()
is_idle            = False
monitor_running    = True
discord_username   = DISCORD_USERNAME
stopped_sent       = False  # Prevent duplicate stopped notifications


# ==========================================================
#  Input Listeners
# ==========================================================

def _reset_activity():
    """Called on any mouse or keyboard event."""
    global last_activity_time, is_idle
    last_activity_time = time.time()

    if is_idle:
        is_idle = False
        log.info("User is ACTIVE again.")
        send_status("active")


def on_move(x, y):            _reset_activity()
def on_click(x, y, btn, pr): _reset_activity()
def on_scroll(x, y, dx, dy): _reset_activity()
def on_key_press(key):        _reset_activity()


# ==========================================================
#  HTTP Communication
# ==========================================================

def send_status(status: str, idle_minutes: float = 0):
    """
    Send activity status to the bot server.
    status: "active" | "idle" | "heartbeat" | "started" | "stopped"
    """
    if not SECRET_TOKEN:
        log.error("❌ SECRET_TOKEN is not set in vdmonitor_config.env — cannot send status.")
        return

    if not BOT_SERVER_URL or "yourcompany" in BOT_SERVER_URL:
        log.error("❌ BOT_SERVER_URL is not configured in vdmonitor_config.env")
        return

    payload = {
        "token":        SECRET_TOKEN,
        "username":     discord_username,
        "status":       status,
        "idle_minutes": round(idle_minutes, 1),
        "timestamp":    datetime.now().astimezone().isoformat(),
        "machine":      (
            os.environ.get("COMPUTERNAME")
            or os.environ.get("HOSTNAME", "unknown")
        ),
    }

    try:
        response = requests.post(BOT_SERVER_URL, json=payload, timeout=10)
        if response.status_code == 200:
            log.info(f"✅ Sent status '{status}' to bot server.")
        else:
            log.warning(
                f"⚠️ Bot server returned {response.status_code}: {response.text}"
            )
    except requests.exceptions.ConnectionError:
        log.error("❌ Cannot reach bot server. Check BOT_SERVER_URL and your internet.")
    except requests.exceptions.Timeout:
        log.error("❌ Bot server timed out.")
    except Exception as e:
        log.error(f"❌ Error sending status: {e}")


# ==========================================================
#  Idle Checker Loop
# ==========================================================

def idle_checker_loop():
    global is_idle
    last_heartbeat = time.time()

    while monitor_running:
        time.sleep(CHECK_INTERVAL_SECONDS)

        elapsed_idle = time.time() - last_activity_time
        idle_minutes = elapsed_idle / 60

        # ── Crossed into idle ─────────────────────────────────────────────
        if elapsed_idle >= IDLE_THRESHOLD_SECONDS and not is_idle:
            is_idle = True
            log.warning(f"🔴 IDLE: {round(idle_minutes, 1)} minutes of no input.")
            send_status("idle", idle_minutes)

        # ── Still idle — update every 5 minutes ───────────────────────────
        elif is_idle:
            if elapsed_idle % 300 < CHECK_INTERVAL_SECONDS:
                log.warning(f"🔴 Still idle: {round(idle_minutes, 1)} minutes.")
                send_status("idle", idle_minutes)

        # ── Active — send heartbeat ────────────────────────────────────────
        else:
            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                last_heartbeat = now
                log.info("💚 Heartbeat: user is active.")
                send_status("heartbeat")


# ==========================================================
#  Username Prompt
# ==========================================================

def prompt_username() -> str:
    """Show a GUI dialog to get the Discord username on first run."""
    try:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        username = simpledialog.askstring(
            "VD Monitor",
            "Enter your Discord username (e.g. John):\n\n"
            "This is used to identify you in Discord alerts.",
            parent=root,
        )
        root.destroy()
        if username and username.strip():
            return username.strip()
    except Exception as e:
        log.warning(f"GUI dialog failed: {e}")

    # CLI fallback (won't work in windowed mode)
    try:
        print("=" * 50)
        print("  🌿 VD Activity Monitor")
        print("=" * 50)
        return input("Enter your Discord username: ").strip()
    except (EOFError, RuntimeError):
        log.error("❌ Cannot get username (no input available in windowed mode)")
        log.error("   Edit vdmonitor_config.env and set DISCORD_USERNAME=YourName")
        return "unknown"


def save_username(username: str):
    """
    Save the entered username back into the config file
    so the user is not prompted again next time.
    """
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).parent
    else:
        base_dir = Path(__file__).parent

    env_file = base_dir / "vdmonitor_config.env"

    if env_file.exists():
        lines = env_file.read_text(encoding="utf-8").splitlines()
        new_lines = []
        found = False
        for line in lines:
            if line.strip().startswith("DISCORD_USERNAME="):
                new_lines.append(f"DISCORD_USERNAME={username}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"DISCORD_USERNAME={username}")
        env_file.write_text("\n".join(new_lines), encoding="utf-8")
        log.info(f"[CONFIG] Saved username '{username}' to config file.")


# ==========================================================
#  System Tray Icon
# ==========================================================

def create_tray_icon():
    """Create a simple green-circle system tray icon."""
    if not TRAY_AVAILABLE:
        return None

    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([8, 8, 56, 56], fill=(46, 204, 113, 255))

    def on_quit(icon, item):
        global monitor_running, stopped_sent
        monitor_running = False
        if not stopped_sent:
            stopped_sent = True
            send_status("stopped")
        log.info("🛑 Monitor stopped by user.")
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(
            f"🌿 VD Monitor — {discord_username}",
            lambda: None,
            enabled=False,
        ),
        pystray.MenuItem("Quit", on_quit),
    )

    return pystray.Icon("vdmonitor", img, "🌿 VD Monitor", menu)


# ==========================================================
#  Main
# ==========================================================

def main():
    global discord_username, monitor_running, stopped_sent

    # ── Validate config ───────────────────────────────────────────────────
    if not SECRET_TOKEN:
        log.error(
            "❌ SECRET_TOKEN is missing from vdmonitor_config.env\n"
            "   Ask your admin for the config file."
        )
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("VD Monitor", "SECRET_TOKEN is missing from vdmonitor_config.env\nAsk your admin for the config file.")
            root.destroy()
        except Exception as e:
            log.warning(f"GUI dialog failed: {e}")
        sys.exit(1)

    # ── Get username ──────────────────────────────────────────────────────
    if not discord_username:
        discord_username = prompt_username()
        if not discord_username:
            log.error("No username provided. Exiting.")
            sys.exit(1)
        # Save it so they are not asked again
        save_username(discord_username)

    log.info(f"🌿 VD Monitor starting")
    log.info(f"   User       : {discord_username}")
    log.info(f"   Server URL : {BOT_SERVER_URL}")
    log.info(f"   Idle after : {IDLE_THRESHOLD_SECONDS // 60} minutes")
    log.info(f"   Log file   : {LOG_FILE}")

    # Send startup signal
    send_status("started")

    # Start idle checker thread
    checker = threading.Thread(target=idle_checker_loop, daemon=True)
    checker.start()

    # Start input listeners
    mouse_listener    = mouse.Listener(
        on_move=on_move, on_click=on_click, on_scroll=on_scroll
    )
    keyboard_listener = keyboard.Listener(on_press=on_key_press)

    mouse_listener.start()
    keyboard_listener.start()

    log.info("✅ Monitoring input. Check your system tray.")

    # System tray blocks until quit
    tray = create_tray_icon()
    if tray:
        tray.run()
    else:
        try:
            while monitor_running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    # Cleanup
    monitor_running = False
    if not stopped_sent:
        stopped_sent = True
        send_status("stopped")
    mouse_listener.stop()
    keyboard_listener.stop()
    log.info("🛑 VD Monitor stopped.")


if __name__ == "__main__":
    main()