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
import tkinter as tk
from tkinter import messagebox
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
#  Base Directory (for exe or script)
# ==========================================================

if getattr(sys, "frozen", False):
    # Running as a PyInstaller .exe
    base_dir = Path(sys.executable).parent
else:
    base_dir = Path(__file__).parent

# ==========================================================
#  Configuration (Integrated)
# ==========================================================

# BOT_SERVER_URL = "http://192.168.10.20:8765/activity"
BOT_SERVER_URL = "https://vdmonitorherbsaremyworld.duckdns.org/activity"
SECRET_TOKEN = "156229bdfadf2e9563f50cfc6a568308be9256e4d441f07a5007ca72dd991d15"
DISCORD_USERNAME = ""  # Will be prompted if empty
IDLE_THRESHOLD_MINUTES = 10
CHECK_INTERVAL_SECONDS = 30
HEARTBEAT_INTERVAL_MINUTES = 2

# Computed values
IDLE_THRESHOLD_SECONDS = IDLE_THRESHOLD_MINUTES * 60
HEARTBEAT_INTERVAL_SECONDS = HEARTBEAT_INTERVAL_MINUTES * 60

LOG_FILE = os.path.join(base_dir, "vdmonitor.log")


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
monitor_running    = False
discord_username   = DISCORD_USERNAME
stopped_sent       = False  # Prevent duplicate stopped notifications

mouse_listener    = None
keyboard_listener = None
checker_thread    = None


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
        log.error("❌ SECRET_TOKEN is not set — cannot send status.")
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
            if elapsed_idle % 600 < CHECK_INTERVAL_SECONDS:
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
#  Start / Stop Monitoring Logic
# ==========================================================

def start_monitoring():
    """Start all listeners and the idle checker thread."""
    global monitor_running, stopped_sent, last_activity_time
    global mouse_listener, keyboard_listener, checker_thread
    global is_idle

    monitor_running    = True
    stopped_sent       = False
    is_idle            = False
    last_activity_time = time.time()

    send_status("started")

    checker_thread = threading.Thread(target=idle_checker_loop, daemon=True)
    checker_thread.start()

    mouse_listener = mouse.Listener(
        on_move=on_move, on_click=on_click, on_scroll=on_scroll
    )
    keyboard_listener = keyboard.Listener(on_press=on_key_press)

    mouse_listener.start()
    keyboard_listener.start()

    log.info("✅ Monitoring started.")


def stop_monitoring():
    """Stop all listeners and send stopped signal."""
    global monitor_running, stopped_sent, mouse_listener, keyboard_listener

    if not monitor_running:
        return

    monitor_running = False

    if not stopped_sent:
        stopped_sent = True
        send_status("stopped")

    if mouse_listener:
        mouse_listener.stop()
        mouse_listener = None

    if keyboard_listener:
        keyboard_listener.stop()
        keyboard_listener = None

    log.info("🛑 Monitoring stopped.")


# ==========================================================
#  Username Prompt
# ==========================================================

def prompt_username() -> str | None:
    """Show a GUI dialog to get the Discord username on first run."""
    try:
        from tkinter import simpledialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        while True:
            # username = simpledialog.askstring(
            #     "VD Monitor",
            #     "Enter your Discord username (e.g. John):\n\n"
            #     "This is used to identify you in Discord alerts.",
            #     parent=root,
            # )
            username = simpledialog.askstring(
                "VD Monitor",
                "Arise!\n"
                "Speak, warrior. How shall you be known in these lands?\n\n"
                "State your name!",
                parent=root,
            )

            # User closed the dialog or clicked Cancel
            if username is None:
                root.destroy()
                log.info("Username prompt was closed by user. Exiting.")
                return None

            username = username.strip()

            # Name is mandatory
            if username:
                root.destroy()
                return username

            messagebox.showwarning(
                "VD Monitor",
                "Discord username is required to continue."
            )

    except Exception as e:
        log.warning(f"GUI dialog failed: {e}")

    # CLI fallback (won't work in windowed mode)
    try:
        print("=" * 50)
        print("  🌿 VD Activity Monitor")
        print("=" * 50)

        while True:
            username = input("Enter your Discord username: ").strip()
            if username:
                return username
            print("Discord username is required.")
    except (EOFError, RuntimeError):
        log.error("❌ Cannot get username (no input available in windowed mode)")
        log.error("   Please run the script in a terminal to configure username.")
        return None


# ==========================================================
#  System Tray Icon
# ==========================================================

def create_tray_icon():
    """Create a system tray icon from the company logo."""
    if not TRAY_AVAILABLE:
        return None

    # ── Option A: Load from embedded ico file ─────────────────
    try:
        if getattr(sys, "frozen", False):
            # Running as a PyInstaller .exe
            # In --onefile mode, bundled files are extracted to sys._MEIPASS
            base_path = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        else:
            # Running as normal .py script
            base_path = Path(__file__).parent

        icon_path = base_path / "vdmonitor.ico"
        img = Image.open(icon_path).convert("RGBA")

    except Exception as e:
        log.warning(f"Could not load tray icon from vdmonitor.ico: {e}")

        # ── Option B: Fallback to green circle if icon not found ──
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
#  GUI
# ==========================================================

class VDMonitorApp:
    def __init__(self, root: tk.Tk, username: str):
        self.root     = root
        self.username = username

        self.root.title("🌿 VD Monitor")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        # Intercept the window X button so we always send "stopped" cleanly
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_consent_screen()

    # ── Consent Screen ─────────────────────────────────────────────────────

    def _build_consent_screen(self):
        """Build the initial consent + start screen."""
        self._clear()

        frame = tk.Frame(self.root, padx=30, pady=25)
        frame.pack()

        tk.Label(
            frame,
            text="🌿 VD Monitor",
            font=("Segoe UI", 16, "bold"),
        ).pack(pady=(0, 10))

        tk.Label(
            frame,
            text=(
                "This application monitors your keyboard and mouse\n"
                "activity and reports your status to your team's\n"
                "Discord server.\n\n"
                "No keystrokes or personal data are recorded —\n"
                "only whether you are active or idle."
            ),
            font=("Segoe UI", 10),
            justify="center",
        ).pack(pady=(0, 15))

        tk.Label(
            frame,
            text=f"Logged in as:  {self.username}",
            font=("Segoe UI", 10, "italic"),
            fg="#555555",
        ).pack(pady=(0, 15))

        # Consent checkbox — Start button stays disabled until ticked
        self.consent_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            frame,
            text="I understand and agree to be monitored and stream my screen as part of Company's customer safety policy",
            variable=self.consent_var,
            font=("Segoe UI", 10),
            command=self._on_consent_toggle,
        ).pack(pady=(0, 20))

        # Start button (disabled until checkbox ticked)
        self.start_btn = tk.Button(
            frame,
            text="Start Monitoring",
            font=("Segoe UI", 11, "bold"),
            bg="#2ECC71",
            fg="white",
            width=20,
            state=tk.DISABLED,
            command=self._on_start,
        )
        self.start_btn.pack()

    def _on_consent_toggle(self):
        """Enable or disable the Start button based on the checkbox."""
        if self.consent_var.get():
            self.start_btn.config(state=tk.NORMAL)
        else:
            self.start_btn.config(state=tk.DISABLED)

    # ── Monitoring Screen ──────────────────────────────────────────────────

    def _build_monitoring_screen(self):
        """Build the active monitoring screen with a Stop button."""
        self._clear()

        frame = tk.Frame(self.root, padx=30, pady=25)
        frame.pack()

        tk.Label(
            frame,
            text="🌿 VD Monitor",
            font=("Segoe UI", 16, "bold"),
        ).pack(pady=(0, 10))

        tk.Label(
            frame,
            text="● Monitoring active",
            font=("Segoe UI", 12),
            fg="#2ECC71",
        ).pack(pady=(0, 5))

        tk.Label(
            frame,
            text=f"User: {self.username}",
            font=("Segoe UI", 10, "italic"),
            fg="#555555",
        ).pack(pady=(0, 20))

        tk.Button(
            frame,
            text="Stop Monitoring",
            font=("Segoe UI", 11, "bold"),
            bg="#E74C3C",
            fg="white",
            width=20,
            command=self._on_stop,
        ).pack()

    # ── Button Handlers ────────────────────────────────────────────────────

    def _on_start(self):
        """User clicked Start Monitoring."""
        start_monitoring()
        self._build_monitoring_screen()

    def _on_stop(self):
        """User clicked Stop Monitoring — return to consent screen."""
        stop_monitoring()
        # Return to consent screen so they can restart if they want
        self._build_consent_screen()

    def _on_close(self):
        """
        User clicked the window X button.
        If monitoring is running, stop it cleanly before closing
        so Discord always receives a 'stopped' signal.
        """
        if monitor_running:
            log.info("Window closed while monitoring — sending stopped signal.")
            stop_monitoring()
        self.root.destroy()

    # ── Utility ───────────────────────────────────────────────────────────

    def _clear(self):
        """Destroy all current widgets so we can rebuild the screen."""
        for widget in self.root.winfo_children():
            widget.destroy()


# ==========================================================
#  Main
# ==========================================================

def main():
    global discord_username, monitor_running, stopped_sent

    # ── Validate config ───────────────────────────────────────────────────
    if not SECRET_TOKEN:
        log.error(
            "❌ SECRET_TOKEN is not set\n"
            "   Contact your admin."
        )
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("VD Monitor", "SECRET_TOKEN is not set\nContact your admin.")
            root.destroy()
        except Exception as e:
            log.warning(f"GUI dialog failed: {e}")
        sys.exit(1)

    # ── Get username ──────────────────────────────────────────────────────
    if not discord_username:
        discord_username = prompt_username()
        if not discord_username:
            log.info("No username provided. Exiting.")
            sys.exit(0)

    log.info(f"🌿 VD Monitor starting")
    log.info(f"   User       : {discord_username}")
    log.info(f"   Server URL : {BOT_SERVER_URL}")
    log.info(f"   Idle after : {IDLE_THRESHOLD_SECONDS // 60} minutes")
    log.info(f"   Log file   : {LOG_FILE}")

    # Launch the GUI — this blocks until the window is closed
    root = tk.Tk()
    app  = VDMonitorApp(root, discord_username)
    root.mainloop()

    # Safety net — if somehow mainloop exits with monitoring still running
    if monitor_running:
        stop_monitoring()

    log.info("🛑 VD Monitor exited.")


if __name__ == "__main__":
    main()