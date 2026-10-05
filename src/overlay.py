"""
Always-on-top control panel for Voice-IA (top-right).
Separate START and STOP buttons (no toggle). Device picker for mic / system audio.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import tkinter as tk
from tkinter import font as tkfont

from .config import ROOT
from .devices import list_capture_sources
from .singleton import acquire_single_instance
from .status_bus import AgentStatus, read_status, write_inbox

log = logging.getLogger("voice_ia.overlay")

COL_BG = "#0e1116"
COL_PANEL = "#161b22"
COL_EDGE = "#2a3340"
COL_TEXT = "#e8eef6"
COL_MUTED = "#8b97a8"
COL_BRAND = "#f0c27a"
COL_LISTEN = "#3ecf8e"
COL_SPEECH = "#5ec8ff"
COL_ARMED = "#ffb020"
COL_OK = "#6ee7a8"
COL_ERR = "#ff6b6b"
COL_START = "#1f6f4a"
COL_START_HI = "#2a9d6a"
COL_STOP = "#8b2e2e"
COL_STOP_HI = "#c44"
COL_BUSY = "#3a4555"
COL_DEV = "#1c2330"
COL_DISABLED = "#2a3038"

STATE_COLOR = {
    "boot": COL_MUTED,
    "listening": COL_LISTEN,
    "speech": COL_SPEECH,
    "heard": COL_SPEECH,
    "armed": COL_ARMED,
    "delivering": COL_ARMED,
    "delivered": COL_OK,
    "ignore": COL_MUTED,
    "error": COL_ERR,
    "offline": COL_ERR,
    "train": COL_ARMED,
}

WIDTH = 320
HEIGHT = 178
MARGIN = 18

DEV_X0, DEV_Y0 = 22, 78
DEV_X1, DEV_Y1 = WIDTH - 22, 102
# Two buttons side by side
START_X0, START_Y0 = 22, 118
START_X1, START_Y1 = 164, 158
STOP_X0, STOP_Y0 = 176, 118
STOP_X1, STOP_Y1 = WIDTH - 22, 158

DEVICES_PATH = ROOT / "outbox" / "devices.json"


class OverlayApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("ИА")
        self.root.geometry(f"{WIDTH}x{HEIGHT}+0+0")
        self.root.configure(bg=COL_BG)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.94)

        self._hwnd: int | None = None
        self._drag_x: int | None = 0
        self._drag_y = 0
        self._hidden_for_game = False
        self._pulse = 0.0
        self._last_status = AgentStatus()
        self._phase = "idle"  # idle | recording | busy
        self._click_guard = 0.0
        self._picker: tk.Toplevel | None = None

        self.canvas = tk.Canvas(
            self.root,
            width=WIDTH,
            height=HEIGHT,
            highlightthickness=0,
            bd=0,
            bg=COL_BG,
        )
        self.canvas.pack(fill="both", expand=True)

        family = self._pick_font(["Bahnschrift", "Segoe UI Semibold", "Segoe UI", "Arial"])
        self.font_brand = tkfont.Font(family=family, size=24, weight="bold")
        self.font_label = tkfont.Font(family=family, size=11, weight="bold")
        self.font_btn = tkfont.Font(family=family, size=13, weight="bold")
        self.font_tiny = tkfont.Font(family="Segoe UI", size=8)
        self.font_dev = tkfont.Font(family="Segoe UI", size=9)

        self._build_static()
        self._bind_drag()
        self.root.after(50, self._place_top_right)
        self.root.after(100, self._capture_hwnd)
        self.root.after(120, self._tick)

    def _pick_font(self, names: list[str]) -> str:
        available = set(tkfont.families())
        for n in names:
            if n in available:
                return n
        return "Segoe UI"

    def _build_static(self) -> None:
        c = self.canvas
        c.create_rectangle(1, 1, WIDTH - 2, HEIGHT - 2, fill=COL_PANEL, outline=COL_EDGE, width=1)
        c.create_rectangle(1, 1, 6, HEIGHT - 2, fill=COL_BRAND, outline="")
        c.create_text(22, 24, anchor="w", text="ИА", fill=COL_BRAND, font=self.font_brand, tags=("brand",))
        c.create_text(70, 18, anchor="w", text="VOICE", fill=COL_MUTED, font=self.font_tiny)
        c.create_text(70, 34, anchor="w", text="локальный агент", fill=COL_MUTED, font=self.font_tiny)

        self.dot = c.create_oval(22, 52, 34, 64, fill=COL_MUTED, outline="")
        self.label_id = c.create_text(44, 58, anchor="w", text="Запуск…", fill=COL_TEXT, font=self.font_label)

        self.dev_bg = c.create_rectangle(
            DEV_X0, DEV_Y0, DEV_X1, DEV_Y1, fill=COL_DEV, outline=COL_EDGE, width=1, tags=("dev",)
        )
        self.dev_text = c.create_text(
            DEV_X0 + 8,
            (DEV_Y0 + DEV_Y1) // 2,
            anchor="w",
            text="Устройство…",
            fill=COL_MUTED,
            font=self.font_dev,
            tags=("dev",),
            width=DEV_X1 - DEV_X0 - 28,
        )
        c.create_text(DEV_X1 - 10, (DEV_Y0 + DEV_Y1) // 2, text="▾", fill=COL_MUTED, font=self.font_dev, tags=("dev",))
        c.tag_bind("dev", "<Button-1>", self._on_device_click)

        self.start_bg = c.create_rectangle(
            START_X0, START_Y0, START_X1, START_Y1, fill=COL_START, outline=COL_EDGE, width=1, tags=("start",)
        )
        self.start_text = c.create_text(
            (START_X0 + START_X1) // 2,
            (START_Y0 + START_Y1) // 2,
            text="START",
            fill=COL_TEXT,
            font=self.font_btn,
            tags=("start",),
        )
        c.tag_bind("start", "<ButtonRelease-1>", self._on_start)

        self.stop_bg = c.create_rectangle(
            STOP_X0, STOP_Y0, STOP_X1, STOP_Y1, fill=COL_DISABLED, outline=COL_EDGE, width=1, tags=("stop",)
        )
        self.stop_text = c.create_text(
            (STOP_X0 + STOP_X1) // 2,
            (STOP_Y0 + STOP_Y1) // 2,
            text="STOP",
            fill=COL_MUTED,
            font=self.font_btn,
            tags=("stop",),
        )
        c.tag_bind("stop", "<ButtonRelease-1>", self._on_stop)

        self.close_id = c.create_text(WIDTH - 16, 14, text="×", fill=COL_MUTED, font=self.font_label)
        c.tag_bind(self.close_id, "<Button-1>", self._on_close)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _paint_buttons(self, phase: str) -> None:
        self._phase = phase
        if phase == "recording":
            self.canvas.itemconfigure(self.start_bg, fill=COL_DISABLED)
            self.canvas.itemconfigure(self.start_text, fill=COL_MUTED)
            self.canvas.itemconfigure(self.stop_bg, fill=COL_STOP)
            self.canvas.itemconfigure(self.stop_text, fill=COL_TEXT)
        elif phase == "busy":
            self.canvas.itemconfigure(self.start_bg, fill=COL_DISABLED)
            self.canvas.itemconfigure(self.start_text, fill=COL_MUTED)
            self.canvas.itemconfigure(self.stop_bg, fill=COL_DISABLED)
            self.canvas.itemconfigure(self.stop_text, fill=COL_MUTED)
        else:
            self.canvas.itemconfigure(self.start_bg, fill=COL_START)
            self.canvas.itemconfigure(self.start_text, fill=COL_TEXT)
            self.canvas.itemconfigure(self.stop_bg, fill=COL_DISABLED)
            self.canvas.itemconfigure(self.stop_text, fill=COL_MUTED)

    def _in_controls(self, x: int, y: int) -> bool:
        if DEV_X0 <= x <= DEV_X1 and DEV_Y0 <= y <= DEV_Y1:
            return True
        if START_X0 <= x <= START_X1 and START_Y0 <= y <= START_Y1:
            return True
        if STOP_X0 <= x <= STOP_X1 and STOP_Y0 <= y <= STOP_Y1:
            return True
        return False

    def _on_start(self, _event: tk.Event) -> None:
        now = time.monotonic()
        if now - self._click_guard < 0.5:
            return
        if self._phase != "idle":
            return
        self._click_guard = now
        self._paint_buttons("recording")
        write_inbox("ptt_start")
        log.info("UI → ptt_start")

    def _on_stop(self, _event: tk.Event) -> None:
        now = time.monotonic()
        if now - self._click_guard < 0.5:
            return
        if self._phase != "recording":
            return
        self._click_guard = now
        self._paint_buttons("busy")
        write_inbox("ptt_stop")
        log.info("UI → ptt_stop")

    def _on_close(self, _event: object | None = None) -> None:
        """Close HUD and ask the agent process to exit."""
        try:
            write_inbox("ptt_stop")
            write_inbox("shutdown")
            log.info("UI → shutdown (close)")
        except Exception:
            pass
        try:
            if self._picker is not None:
                self._picker.destroy()
        except Exception:
            pass
        self.root.destroy()

    def _load_devices(self) -> list[dict]:
        try:
            return [s.to_dict() for s in list_capture_sources()]
        except Exception:
            try:
                if DEVICES_PATH.exists():
                    data = json.loads(DEVICES_PATH.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        return [d for d in data if isinstance(d, dict)]
            except Exception:
                pass
        return []

    def _on_device_click(self, _event: tk.Event) -> None:
        if self._phase != "idle":
            return
        self._open_picker()

    def _open_picker(self) -> None:
        if self._picker is not None:
            try:
                self._picker.destroy()
            except Exception:
                pass
            self._picker = None

        devices = self._load_devices()
        if not devices:
            return

        win = tk.Toplevel(self.root)
        self._picker = win
        win.title("Источник")
        win.configure(bg=COL_PANEL)
        win.attributes("-topmost", True)
        win.overrideredirect(True)

        rx = self.root.winfo_x()
        ry = self.root.winfo_y() + HEIGHT - 4
        win.geometry(f"{WIDTH}x{min(280, 28 + 26 * min(len(devices), 9))}+{rx}+{ry}")

        frame = tk.Frame(win, bg=COL_PANEL)
        frame.pack(fill="both", expand=True, padx=4, pady=4)

        canvas = tk.Canvas(frame, bg=COL_PANEL, highlightthickness=0)
        scroll = tk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=COL_PANEL)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        cur = (self._last_status.device_key or "").strip()

        def pick(key: str) -> None:
            write_inbox("set_device", device_key=key)
            try:
                win.destroy()
            except Exception:
                pass
            self._picker = None

        for d in devices:
            key = str(d.get("key") or "")
            label = str(d.get("label") or d.get("name") or key)
            fg = COL_BRAND if key == cur else COL_TEXT
            b = tk.Button(
                inner,
                text=label,
                anchor="w",
                bg=COL_DEV,
                fg=fg,
                activebackground=COL_EDGE,
                activeforeground=COL_TEXT,
                relief="flat",
                font=self.font_dev,
                command=lambda k=key: pick(k),
            )
            b.pack(fill="x", pady=1, padx=2)

        def close_picker(_e=None) -> None:
            try:
                win.destroy()
            except Exception:
                pass
            self._picker = None

        win.bind("<Escape>", close_picker)
        win.after(15000, close_picker)

    def _bind_drag(self) -> None:
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_motion)

    def _on_drag_start(self, event: tk.Event) -> None:
        if self._in_controls(event.x, event.y):
            self._drag_x = None
            return
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _on_drag_motion(self, event: tk.Event) -> None:
        if self._drag_x is None:
            return
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _place_top_right(self) -> None:
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        x = sw - WIDTH - MARGIN
        y = MARGIN + 8
        self.root.geometry(f"{WIDTH}x{HEIGHT}+{x}+{y}")

    def _capture_hwnd(self) -> None:
        try:
            import win32con
            import win32gui

            hwnd = int(self.root.winfo_id())
            parent = win32gui.GetParent(hwnd)
            while parent:
                hwnd = parent
                parent = win32gui.GetParent(hwnd)
            self._hwnd = hwnd
            ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            ex |= win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_TOPMOST
            ex &= ~win32con.WS_EX_APPWINDOW
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex)
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_TOPMOST,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW | win32con.SWP_FRAMECHANGED,
            )
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
        except Exception as exc:
            log.debug("hwnd capture: %s", exc)

    def _short_device(self, label: str, n: int = 36) -> str:
        t = (label or "").replace("\n", " ").strip() or "Устройство…"
        if len(t) <= n:
            return t
        return t[: n - 1] + "…"

    def _apply_status(self, st: AgentStatus) -> None:
        color = STATE_COLOR.get(st.state, COL_MUTED)
        if st.state == "listening":
            self._pulse = (self._pulse + 0.12) % 1.0
            color = COL_LISTEN if self._pulse < 0.5 else "#2a9d6a"
        self.canvas.itemconfigure(self.dot, fill=color)
        self.canvas.itemconfigure(self.label_id, text=st.label or st.state, fill=COL_TEXT)
        self.canvas.itemconfigure(
            self.dev_text,
            text=self._short_device(st.device_label),
            fill=COL_TEXT if st.device_label else COL_MUTED,
        )

        # Sync buttons from agent — but never fight a very recent local click
        if time.monotonic() - self._click_guard < 0.6:
            self._last_status = st
            return

        if st.btn == "busy" or st.state in ("boot", "delivering"):
            phase = "busy"
        elif st.armed or st.state == "speech" or st.btn == "stop":
            phase = "recording"
        else:
            phase = "idle"
        self._paint_buttons(phase)
        self._last_status = st

    def _tick(self) -> None:
        try:
            st = read_status()
            if st.state != "offline" and (time.time() - st.ts) > 12:
                st = AgentStatus(state="offline", label="Нет сигнала от агента", btn="busy")
            self._apply_status(st)
        except Exception as exc:
            log.debug("status tick: %s", exc)

        try:
            in_game = _is_fullscreen_game_foreground(self._hwnd)
            if in_game and not self._hidden_for_game:
                self.root.withdraw()
                self._hidden_for_game = True
            elif not in_game and self._hidden_for_game:
                self.root.deiconify()
                self.root.attributes("-topmost", True)
                self._capture_hwnd()
                self._hidden_for_game = False
            elif not in_game:
                self.root.attributes("-topmost", True)
        except Exception:
            pass

        self.root.after(200, self._tick)

    def run(self) -> None:
        self.root.mainloop()


def _is_fullscreen_game_foreground(overlay_hwnd: int | None) -> bool:
    try:
        import win32api
        import win32con
        import win32gui
    except ImportError:
        return False

    fg = win32gui.GetForegroundWindow()
    if not fg or (overlay_hwnd and fg == overlay_hwnd):
        return False

    try:
        title = win32gui.GetWindowText(fg) or ""
        cls = win32gui.GetClassName(fg) or ""
    except Exception:
        return False
    if cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Button"):
        return False
    if "voice-ia" in title.lower() or title == "ИА":
        return False

    try:
        left, top, right, bottom = win32gui.GetWindowRect(fg)
    except Exception:
        return False
    w, h = right - left, bottom - top
    if w < 800 or h < 600:
        return False

    mon = win32api.MonitorFromWindow(fg, win32con.MONITOR_DEFAULTTONEAREST)
    info = win32api.GetMonitorInfo(mon)
    ml, mt, mr, mb = info["Monitor"]
    mw, mh = mr - ml, mb - mt
    cover_w = min(right, mr) - max(left, ml)
    cover_h = min(bottom, mb) - max(top, mt)
    if cover_w <= 0 or cover_h <= 0:
        return False
    ratio = (cover_w * cover_h) / float(mw * mh)
    style = win32gui.GetWindowLong(fg, win32con.GWL_STYLE)
    has_caption = bool(style & win32con.WS_CAPTION)
    return ratio >= 0.97 and (not has_caption or ratio >= 0.995)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    if not acquire_single_instance("Local\\VoiceIA_Overlay_Mutex"):
        return 2
    from .status_bus import StatusWriter

    if not (ROOT / "outbox" / "status.json").exists():
        StatusWriter().set_state("offline", "Ожидание агента…", btn="busy")
    OverlayApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
