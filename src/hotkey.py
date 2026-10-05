from __future__ import annotations

"""Global push-to-talk hotkey with consume (other apps must not see Alt+S)."""

import atexit
import ctypes
import logging
import threading
from ctypes import wintypes
from dataclasses import dataclass

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13
WM_QUIT = 0x0012
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5

LLKHF_UP = 0x80

_KEY_MAP = {
    "s": 0x53,
    "a": 0x41,
    "z": 0x5A,
    "x": 0x58,
    "c": 0x43,
    "v": 0x56,
    "q": 0x51,
    "w": 0x57,
    "e": 0x45,
    "r": 0x52,
    "t": 0x54,
    "f": 0x46,
    "g": 0x47,
    "h": 0x48,
    "space": 0x20,
}

_ALT_VKS = {VK_MENU, VK_LMENU, VK_RMENU}
_CTRL_VKS = {VK_CONTROL, VK_LCONTROL, VK_RCONTROL}
_SHIFT_VKS = {VK_SHIFT, VK_LSHIFT, VK_RSHIFT}

LRESULT = ctypes.c_ssize_t
HHOOK = ctypes.c_void_p


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


LowLevelKeyboardProc = ctypes.WINFUNCTYPE(
    LRESULT,
    ctypes.c_int,
    wintypes.WPARAM,
    wintypes.LPARAM,
)

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, LowLevelKeyboardProc, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = HHOOK
user32.CallNextHookEx.argtypes = [HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD



@dataclass
class HotkeySpec:
    """e.g. alt+s"""

    alt: bool = True
    ctrl: bool = False
    shift: bool = False
    key: str = "s"

    @classmethod
    def parse(cls, text: str) -> "HotkeySpec":
        parts = [p.strip().lower() for p in (text or "alt+s").replace("-", "+").split("+") if p.strip()]
        alt = "alt" in parts
        ctrl = "ctrl" in parts or "control" in parts
        shift = "shift" in parts
        key = next((p for p in parts if p not in ("alt", "ctrl", "control", "shift")), "s")
        return cls(alt=alt, ctrl=ctrl, shift=shift, key=key)

    def vk(self) -> int:
        mapped = _KEY_MAP.get(self.key)
        if mapped is not None:
            return mapped
        if len(self.key) == 1 and "a" <= self.key <= "z":
            return ord(self.key.upper())
        raise ValueError(f"Unsupported hotkey key: {self.key!r}")

    def label(self) -> str:
        bits = []
        if self.ctrl:
            bits.append("Ctrl")
        if self.alt:
            bits.append("Alt")
        if self.shift:
            bits.append("Shift")
        bits.append(self.key.upper())
        return "+".join(bits)

    def is_pressed(self) -> bool:
        """Fallback poll (no consume). Prefer PttHotkeyGrabber.is_pressed."""
        if self.alt and not (
            user32.GetAsyncKeyState(VK_MENU) & 0x8000
            or user32.GetAsyncKeyState(VK_LMENU) & 0x8000
            or user32.GetAsyncKeyState(VK_RMENU) & 0x8000
        ):
            return False
        if self.ctrl and not (
            user32.GetAsyncKeyState(VK_CONTROL) & 0x8000
            or user32.GetAsyncKeyState(VK_LCONTROL) & 0x8000
            or user32.GetAsyncKeyState(VK_RCONTROL) & 0x8000
        ):
            return False
        if self.shift and not (
            user32.GetAsyncKeyState(VK_SHIFT) & 0x8000
            or user32.GetAsyncKeyState(VK_LSHIFT) & 0x8000
            or user32.GetAsyncKeyState(VK_RSHIFT) & 0x8000
        ):
            return False
        return bool(user32.GetAsyncKeyState(self.vk()) & 0x8000)


class PttHotkeyGrabber:
    """
    Low-level keyboard hook: while agent runs, Alt+S (or configured combo)
    is swallowed so Word/others do not receive it. Hold = PTT active.
    """

    def __init__(self, spec: HotkeySpec) -> None:
        self.spec = spec
        self._vk = spec.vk()
        self._pressed = False
        self._alt = False
        self._ctrl = False
        self._shift = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._tid = 0
        self._hook = None
        self._proc = None
        self._running = False

    @property
    def label(self) -> str:
        return self.spec.label()

    def is_pressed(self) -> bool:
        with self._lock:
            return self._pressed

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._thread_main, name="ptt-hotkey", daemon=True)
        self._thread.start()
        # Wait until hook installed (or failed)
        for _ in range(100):
            if self._hook or not self._running:
                break
            threading.Event().wait(0.02)
        if not self._hook:
            raise RuntimeError("Failed to install keyboard hook for PTT hotkey")
        atexit.register(self.stop)
        log.info("PTT hotkey grabber active: %s (consumed globally)", self.label)

    def stop(self) -> None:
        if not self._running and not self._hook:
            return
        self._running = False
        tid = self._tid
        if tid:
            try:
                user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:
                pass
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._thread = None
        with self._lock:
            self._pressed = False
        log.info("PTT hotkey grabber stopped")

    def _mods_match(self) -> bool:
        if self.spec.alt and not self._alt:
            return False
        if self.spec.ctrl and not self._ctrl:
            return False
        if self.spec.shift and not self._shift:
            return False
        # If combo does not require a modifier, still OK
        if not self.spec.alt and self._alt:
            # bare key while Alt held — ignore (would be Alt+key for OS)
            return False
        if not self.spec.ctrl and self._ctrl:
            return False
        if not self.spec.shift and self._shift:
            return False
        return True

    def _set_pressed(self, value: bool) -> None:
        with self._lock:
            self._pressed = value

    def _thread_main(self) -> None:
        self._tid = kernel32.GetCurrentThreadId()
        grabber = self

        @LowLevelKeyboardProc
        def _proc(n_code: int, w_param: int, l_param: int) -> int:
            try:
                if n_code == 0 and l_param:
                    info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    if grabber._handle(int(w_param), info):
                        return 1  # swallow — do not pass to other apps
            except Exception:
                log.exception("PTT hook error")
            return int(user32.CallNextHookEx(grabber._hook, n_code, w_param, l_param) or 0)

        self._proc = _proc  # keep reference alive
        # WH_KEYBOARD_LL: hMod may be NULL when callback is in this process.
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL,
            self._proc,
            wintypes.HINSTANCE(0),
            0,
        )
        if not self._hook:
            err = ctypes.get_last_error()
            log.error("SetWindowsHookEx failed err=%s", err)
            self._running = False
            return

        msg = wintypes.MSG()
        while self._running:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0 or ret == -1:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    def _handle(self, w_param: int, info: KBDLLHOOKSTRUCT) -> bool:
        """Return True to swallow the event."""
        vk = int(info.vkCode)
        is_up = bool(info.flags & LLKHF_UP) or w_param in (WM_KEYUP, WM_SYSKEYUP)
        is_down = not is_up

        # Track modifiers (never swallow modifier keys alone — typing Alt menus still works
        # until the letter of our combo is pressed; that letter is swallowed).
        if vk in _ALT_VKS:
            self._alt = is_down
            if is_up and self.is_pressed():
                self._set_pressed(False)
            return False
        if vk in _CTRL_VKS:
            self._ctrl = is_down
            if is_up and self.is_pressed():
                self._set_pressed(False)
            return False
        if vk in _SHIFT_VKS:
            self._shift = is_down
            if is_up and self.is_pressed():
                self._set_pressed(False)
            return False

        if vk != self._vk:
            return False

        # Our trigger key
        if is_down:
            if self._mods_match() or self.is_pressed():
                self._set_pressed(True)
                return True  # swallow S (and repeats) while Alt held / PTT active
            return False

        # Key up
        if self.is_pressed() or self._mods_match():
            self._set_pressed(False)
            return True
        return False
