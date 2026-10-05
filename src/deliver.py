from __future__ import annotations

import ctypes
import json
import logging
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

import pyperclip

log = logging.getLogger(__name__)

try:
    import win32api
    import win32clipboard
    import win32con
    import win32gui
    import win32process
except ImportError:  # pragma: no cover
    win32api = None  # type: ignore
    win32clipboard = None  # type: ignore
    win32con = None  # type: ignore
    win32gui = None  # type: ignore
    win32process = None  # type: ignore

# --- SendInput (more reliable than keybd_event for Electron) ---
ULONG_PTR = ctypes.c_size_t
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_EXTENDEDKEY = 0x0001


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT(ctypes.Structure):
    class _INPUT(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    _anonymous_ = ("_input",)
    _fields_ = [("type", wintypes.DWORD), ("_input", _INPUT)]


def _send_input(*inputs: INPUT) -> None:
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = ctypes.windll.user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(INPUT))
    if sent != n:
        raise RuntimeError(f"SendInput sent {sent}/{n}")


def _key_vk(vk: int, up: bool = False) -> INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0))


def _key_unicode(char: str, up: bool = False) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return INPUT(
        type=INPUT_KEYBOARD,
        ki=KEYBDINPUT(wVk=0, wScan=ord(char), dwFlags=flags, time=0, dwExtraInfo=0),
    )


def append_outbox(path: Path, command: str, transcript: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "transcript": transcript,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def set_clipboard(text: str) -> None:
    last_err: Exception | None = None
    for _ in range(5):
        try:
            if win32clipboard is not None:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
                finally:
                    win32clipboard.CloseClipboard()
                return
            pyperclip.copy(text)
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.05)
    if last_err:
        raise last_err


def _window_title(hwnd: int) -> str:
    try:
        return win32gui.GetWindowText(hwnd) or ""
    except Exception:  # noqa: BLE001
        return ""


def find_cursor_hwnd(title_contains: str = "Cursor") -> int | None:
    if win32gui is None:
        return None
    needle = title_contains.lower()
    found: list[int] = []

    def enum_handler(hwnd: int, _ctx) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = _window_title(hwnd)
        if needle in title.lower() and title.strip():
            found.append(hwnd)

    win32gui.EnumWindows(enum_handler, None)
    if not found:
        return None

    def score(h: int) -> tuple:
        title = _window_title(h).lower()
        prefer = 0
        if "agent" in title:
            prefer = -2
        elif "chat" in title or "composer" in title:
            prefer = -1
        return (prefer, 0 if "cursor" in title else 1, -len(title))

    return sorted(found, key=score)[0]


def focus_window(hwnd: int) -> bool:
    if win32gui is None or win32con is None or win32api is None:
        return False
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        if win32process is not None:
            fg = win32gui.GetForegroundWindow()
            cur_tid = win32api.GetCurrentThreadId()
            fg_tid, _ = win32process.GetWindowThreadProcessId(fg) if fg else (0, 0)
            tgt_tid, _ = win32process.GetWindowThreadProcessId(hwnd)
            if fg_tid and fg_tid != cur_tid:
                win32process.AttachThreadInput(cur_tid, fg_tid, True)
            if tgt_tid and tgt_tid != cur_tid:
                win32process.AttachThreadInput(cur_tid, tgt_tid, True)
            try:
                win32gui.BringWindowToTop(hwnd)
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                win32gui.SetForegroundWindow(hwnd)
            finally:
                if tgt_tid and tgt_tid != cur_tid:
                    win32process.AttachThreadInput(cur_tid, tgt_tid, False)
                if fg_tid and fg_tid != cur_tid:
                    win32process.AttachThreadInput(cur_tid, fg_tid, False)
        else:
            win32gui.SetForegroundWindow(hwnd)
        return win32gui.GetForegroundWindow() == hwnd
    except Exception as exc:  # noqa: BLE001
        log.warning("Focus failed: %s", exc)
        try:
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
            win32gui.SetForegroundWindow(hwnd)
            return True
        except Exception as exc2:  # noqa: BLE001
            log.warning("Focus retry failed: %s", exc2)
            return False


def _click_screen(x: int, y: int) -> None:
    if win32api is None or win32con is None:
        return
    win32api.SetCursorPos((int(x), int(y)))
    time.sleep(0.03)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.02)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def click_chat_input(
    hwnd: int,
    *,
    offset_from_bottom: int = 160,
    x_ratio: float = 0.30,
) -> tuple[int, int]:
    """Click Agents 'Send follow-up' composer (chat column, not window center)."""
    if win32gui is None:
        return (0, 0)
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width = max(1, right - left)
    x = left + int(width * max(0.05, min(0.95, x_ratio)))
    y = bottom - max(40, offset_from_bottom)
    y = min(max(y, top + 100), bottom - 25)
    log.info("Click composer screen=(%s,%s) x_ratio=%.2f bottom_offset=%s", x, y, x_ratio, offset_from_bottom)
    _click_screen(x, y)
    time.sleep(0.06)
    _click_screen(x, y)
    return (x, y)


def send_paste(auto_enter: bool = False) -> None:
    vk_control = 0x11
    vk_v = 0x56
    vk_shift = 0x10
    vk_insert = 0x2D
    vk_return = 0x0D
    _send_input(_key_vk(vk_control), _key_vk(vk_v), _key_vk(vk_v, True), _key_vk(vk_control, True))
    time.sleep(0.04)
    _send_input(_key_vk(vk_shift), _key_vk(vk_insert), _key_vk(vk_insert, True), _key_vk(vk_shift, True))
    if auto_enter:
        time.sleep(0.06)
        _send_input(_key_vk(vk_return), _key_vk(vk_return, True))


def type_text_unicode(text: str, auto_enter: bool = False, *, delay_s: float = 0.012) -> None:
    """Type Unicode text via SendInput. delay_s helps Word/Notepad keep up."""
    batch: list[INPUT] = []
    for ch in text:
        if ch == "\n":
            if batch:
                _send_input(*batch)
                batch.clear()
                time.sleep(delay_s)
            _send_input(_key_vk(0x0D), _key_vk(0x0D, True))
            time.sleep(delay_s)
            continue
        batch.append(_key_unicode(ch, False))
        batch.append(_key_unicode(ch, True))
        # Small batches + pause — long bursts drop chars in Word/Electron
        if len(batch) >= 16:
            _send_input(*batch)
            batch.clear()
            time.sleep(delay_s)
    if batch:
        _send_input(*batch)
        time.sleep(delay_s)
    if auto_enter:
        time.sleep(0.05)
        _send_input(_key_vk(0x0D), _key_vk(0x0D, True))


def beep() -> None:
    try:
        if win32api is not None:
            win32api.MessageBeep(win32con.MB_OK)
        else:
            print("\a", end="", flush=True)
    except Exception:  # noqa: BLE001
        print("\a", end="", flush=True)


def _foreground_hwnd() -> int | None:
    if win32gui is None:
        return None
    try:
        hwnd = win32gui.GetForegroundWindow()
        return int(hwnd) if hwnd else None
    except Exception:  # noqa: BLE001
        return None


def _is_our_hud(hwnd: int | None) -> bool:
    if not hwnd:
        return False
    title = _window_title(hwnd).strip()
    return title in ("ИА", "Voice-IA", "voice-ia") or title.startswith("ИА ")


def deliver_command(
    command: str,
    *,
    transcript: str,
    outbox_path: Path,
    target: str = "active",
    title_contains: str = "Cursor",
    auto_enter: bool = False,
    do_beep: bool = True,
    click_offset_from_bottom: int = 160,
    click_x_ratio: float = 0.30,
    paste_mode: str = "type",
) -> bool:
    """
    Insert command text into the focused app (Word, Notepad, browser, Cursor, …).

    target:
      - active  — type/paste into the current foreground window (default)
      - cursor  — old behavior: find Cursor, click composer, type
    """
    text = command.strip()
    if text.endswith(".") and text.count(".") == 1:
        text = text[:-1].strip()

    append_outbox(outbox_path, text, transcript)
    set_clipboard(text)

    mode = (paste_mode or "type").lower()
    tgt = (target or "active").lower()

    if tgt in ("active", "focus", "foreground", "anywhere"):
        hwnd = _foreground_hwnd()
        title = _window_title(hwnd) if hwnd else ""
        if _is_our_hud(hwnd):
            log.warning("Foreground is Voice-IA HUD — click the target app, then repeat. Clipboard has text.")
            if do_beep:
                beep()
            return False

        # Do not steal focus / do not click — caret stays where the user left it
        time.sleep(0.08)
        # Prefer clipboard paste for long text (Word drops fast SendInput chars)
        if mode in ("type", "click_type"):
            type_text_unicode(text, auto_enter=auto_enter)
            used = "type"
        else:
            # paste / click_paste / default
            send_paste(auto_enter=auto_enter)
            used = "paste"

        if do_beep:
            beep()
        log.info(
            "Delivered to active hwnd=%s title=%r mode=%s chars=%s: %r",
            hwnd,
            title,
            used,
            len(text),
            text,
        )
        return True

    # --- legacy: Cursor Agents composer ---
    hwnd = find_cursor_hwnd(title_contains)
    if hwnd is None:
        log.warning(
            "Cursor window not found (title contains %r). Text is in clipboard + outbox.",
            title_contains,
        )
        if do_beep:
            beep()
        return False

    focused = focus_window(hwnd)
    time.sleep(0.18)
    pos = click_chat_input(
        hwnd,
        offset_from_bottom=click_offset_from_bottom,
        x_ratio=click_x_ratio,
    )
    time.sleep(0.15)

    if mode in ("type", "click_type"):
        type_text_unicode(text, auto_enter=auto_enter)
    elif mode == "paste":
        send_paste(auto_enter=auto_enter)
    else:
        send_paste(auto_enter=False)
        time.sleep(0.05)
        type_text_unicode(text, auto_enter=auto_enter)

    if do_beep:
        beep()
    log.info(
        "Delivered hwnd=%s title=%r focused=%s click=%s mode=%s: %r",
        hwnd,
        _window_title(hwnd),
        focused,
        pos,
        mode,
        text,
    )
    return True


# Back-compat alias
def deliver_to_cursor(*args, **kwargs) -> bool:
    if "target" not in kwargs:
        kwargs["target"] = "cursor"
    return deliver_command(*args, **kwargs)
