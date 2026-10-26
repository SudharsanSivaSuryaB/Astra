import sys
import os
import subprocess
import shutil
import threading
import time
import ctypes
import re
import urllib.parse
from ctypes import wintypes
from datetime import datetime
import speech_recognition as sr
import sounddevice as sd
from PySide6.QtCore import Qt, QTimer, QPoint, QThread, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QFont, QAction
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout,
    QMenu, QSystemTrayIcon, QStyle
)

RECOGNITION_LANGUAGE = "en-IN"
CONVERSATION_IDLE_TIMEOUT_SECONDS = 30
FEMALE_TTS_VOICE = "en-US-AriaNeural"
WAKE_WORD_VARIANTS = ("astra", "astro", "a star", "a straw",
    "ASS-truh",
    "AHS-truh",
    "AS-tra",
    "AHSS-tra",
    "UHSS-truh",
    "AS-t ruh",
    "AAS-truh",
    "ASS-trah",
    "AHSS-trah",
    "AZ-truh",
    "AS-dra",
    "ASH-truh",
    "AS-ṭra",
    "A-struh",
    "ASS-tuh-ruh",
    "AS-TRA",
    "AAS-TRA",
    "UHSS-TRA",
    "ASS-trə",
    "AH-stra"
)
EXIT_PHRASES = (
    "goodbye", "good bye", "bye bye", "that's all", "that is all",
    "exit", "quit", "shut down", "close astra", "close the assistant",
)


def is_exit_command(text):
    normalized = text.strip().lower()
    if any(phrase in normalized for phrase in ("exit notes", "exit notepad", "stop notes")):
        return False
    return any(phrase in normalized for phrase in EXIT_PHRASES)


def is_exit_notes_command(text):
    normalized = text.strip().lower()
    return "exit notes" in normalized or "exit notepad" in normalized


def is_stop_notes_command(text):
    normalized = text.strip().lower()
    return any(phrase in normalized for phrase in (
        "stop notes", "staff notes", "top notes", "end notes"
    ))


def find_wake_word(text):
    normalized = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    normalized = " ".join(normalized.split())
    padded = f" {normalized} "
    for variant in WAKE_WORD_VARIANTS:
        if f" {variant} " in padded or normalized == variant:
            return variant
    if normalized == "extra":
        return "extra"
    return ""


def remove_wake_word(text):
    for variant in (*WAKE_WORD_VARIANTS, "extra"):
        if variant == "extra" and text.strip().lower() != "extra":
            continue
        match = re.search(rf"\b{re.escape(variant)}\b", text, flags=re.IGNORECASE)
        if match:
            return (text[:match.start()] + text[match.end():]).strip(" ,.!?")
    return text.strip(" ,.!?")

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

try:
    import pyperclip
except ImportError:
    pyperclip = None


class SpeechWorker(QThread):
    speech_started = Signal(str)

    def __init__(self, text, edge_playback):
        super().__init__()
        self.text = text
        self.edge_playback = edge_playback
        self.success = False
        self.error = ""

    def run(self):
        local_error = "No installed Windows Aria voice was found."
        if pyttsx3:
            try:
                engine = pyttsx3.init()
                voices = engine.getProperty("voices") or []
                aria = next((
                    voice for voice in voices
                    if "aria" in f"{voice.name} {voice.id}".lower()
                ), None)
                if aria is None:
                    engine.stop()
                    raise RuntimeError("Windows does not expose an installed Aria voice to SAPI.")
                engine.setProperty("voice", aria.id)
                engine.setProperty("rate", 190)
                started = {"value": False}

                def on_started(_name):
                    started["value"] = True
                    self.speech_started.emit(self.text)

                engine.connect("started-utterance", on_started)
                engine.say(self.text)
                engine.runAndWait()
                if not started["value"]:
                    self.speech_started.emit(self.text)
                self.success = True
                return
            except (OSError, subprocess.SubprocessError) as exc:
                local_error = str(exc)
            except Exception as exc:
                local_error = str(exc)

        # Use online Aria only when local Windows Aria is unavailable.
        if os.path.isfile(self.edge_playback):
            try:
                self.speech_started.emit(self.text)
                subprocess.run(
                    [
                        self.edge_playback,
                        "--voice", FEMALE_TTS_VOICE,
                        "--rate=+8%",
                        "--pitch=+3Hz",
                        "--text", self.text,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=90,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.success = True
                return
            except (OSError, subprocess.SubprocessError) as exc:
                detail = getattr(exc, "stderr", None)
                online_error = detail.strip() if detail else str(exc)
                self.error = f"{local_error} Online Aria also failed: {online_error}"
                return

        self.error = f"{local_error} Online Aria is not installed."


class Win32MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class Win32KeyBdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class Win32HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class Win32InputUnion(ctypes.Union):
    # INPUT's union must include MOUSEINPUT so its size matches the Win32 ABI.
    _fields_ = [
        ("mi", Win32MouseInput),
        ("ki", Win32KeyBdInput),
        ("hi", Win32HardwareInput),
    ]


class Win32Input(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", wintypes.DWORD), ("data", Win32InputUnion)]


class NotesWindow:
    """Open Notepad, focus it, and type dictated Unicode text through Win32."""
    WM_CLOSE = 0x0010
    SW_RESTORE = 9
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    INPUT_KEYBOARD = 1
    VK_CONTROL = 0x11
    VK_A = 0x41
    VK_V = 0x56
    VK_ENTER = 0x0D
    VK_RIGHT = 0x27
    VK_ESCAPE = 0x1B

    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("Voice notes are supported on Windows only.")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.SendInput.argtypes = (
            wintypes.UINT,
            ctypes.POINTER(Win32Input),
            ctypes.c_int,
        )
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.EnumWindows.argtypes = (
            ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
            wintypes.LPARAM,
        )
        self.user32.EnumWindows.restype = wintypes.BOOL
        self.user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.IsIconic.argtypes = (wintypes.HWND,)
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.IsWindow.argtypes = (wintypes.HWND,)
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        self.user32.BringWindowToTop.argtypes = (wintypes.HWND,)
        self.user32.PostMessageW.argtypes = (
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )
        self.process = subprocess.Popen(["notepad.exe"])
        self.hwnd = None
        for _ in range(60):
            self.hwnd = self._find_window()
            if self.hwnd:
                break
            time.sleep(0.1)
        if not self.hwnd:
            raise RuntimeError("Could not find the Notepad window.")
        self.focus()

    def _find_window(self):
        found = {"hwnd": None, "fallback": None}
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _):
            if not self.user32.IsWindowVisible(hwnd):
                return True
            title_length = self.user32.GetWindowTextLengthW(hwnd)
            if title_length <= 0:
                return True
            title = ctypes.create_unicode_buffer(title_length + 1)
            self.user32.GetWindowTextW(hwnd, title, len(title))
            if "notepad" not in title.value.lower():
                return True
            process_id = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if process_id.value == self.process.pid:
                found["hwnd"] = hwnd
                return False
            if found["fallback"] is None:
                found["fallback"] = hwnd
            return True

        callback = callback_type(visit)
        self.user32.EnumWindows(callback, 0)
        return found["hwnd"] or found["fallback"]

    def focus(self):
        self.hwnd = self._find_window() or self.hwnd
        if not self.hwnd:
            raise RuntimeError("The Notepad window is no longer available.")
        self.user32.ShowWindow(self.hwnd, self.SW_RESTORE)
        self.user32.BringWindowToTop(self.hwnd)
        if (not self.user32.SetForegroundWindow(self.hwnd) and
                self.user32.GetForegroundWindow() != self.hwnd):
            raise RuntimeError("Windows would not focus Notepad. Click its window and try again.")
        time.sleep(0.05)

    def type_text(self, text):
        self.focus()
        if pyperclip is not None:
            self._paste_text(text)
            return

        # Fallback for environments where the clipboard package is missing.
        inputs = []
        for character in text:
            # SendInput's Unicode mode takes UTF-16 code units.
            encoded = character.encode("utf-16-le", errors="replace")
            for index in range(0, len(encoded), 2):
                code_unit = int.from_bytes(encoded[index:index + 2], "little")
                down = Win32Input()
                down.type = self.INPUT_KEYBOARD
                down.ki = Win32KeyBdInput(0, code_unit, self.KEYEVENTF_UNICODE, 0, 0)
                up = Win32Input()
                up.type = self.INPUT_KEYBOARD
                up.ki = Win32KeyBdInput(
                    0, code_unit, self.KEYEVENTF_UNICODE | self.KEYEVENTF_KEYUP, 0, 0
                )
                inputs.extend((down, up))
        if inputs:
            input_array = (Win32Input * len(inputs))(*inputs)
            item_size = ctypes.sizeof(Win32Input)
            sent_total = 0
            chunk_size = 64
            while sent_total < len(input_array):
                chunk_end = min(sent_total + chunk_size, len(input_array))
                chunk_sent = sent_total
                retries = 0
                while chunk_sent < chunk_end:
                    remaining = chunk_end - chunk_sent
                    pointer = ctypes.cast(
                        ctypes.byref(input_array, chunk_sent * item_size),
                        ctypes.POINTER(Win32Input),
                    )
                    sent = self.user32.SendInput(remaining, pointer, item_size)
                    if sent == 0:
                        error_code = ctypes.get_last_error()
                        raise OSError(
                            error_code,
                            "Windows rejected dictated text input. Keep Notepad focused and run Astra and Notepad at the same privilege level.",
                        )
                    chunk_sent += sent
                    retries += 1
                    if retries > chunk_size + 2 and chunk_sent < chunk_end:
                        raise OSError("Windows accepted only part of the dictated text.")
                sent_total = chunk_end
                time.sleep(0.005)

    def _paste_text(self, text):
        try:
            previous_clipboard = pyperclip.paste()
        except Exception:
            previous_clipboard = None

        pyperclip.copy(text)
        keyboard_events = self._hotkey_events(self.VK_CONTROL, self.VK_V)
        try:
            self._send_keyboard_events(keyboard_events)
            time.sleep(0.15)
        finally:
            if previous_clipboard is not None:
                pyperclip.copy(previous_clipboard)

    @staticmethod
    def _hotkey_events(modifier, key):
        return (
            (modifier, 0),
            (key, 0),
            (key, 0x0002),
            (modifier, 0x0002),
        )

    def _send_keyboard_events(self, keyboard_events):
        events = []
        for virtual_key, flags in keyboard_events:
            event = Win32Input()
            event.type = self.INPUT_KEYBOARD
            event.ki = Win32KeyBdInput(virtual_key, 0, flags, 0, 0)
            events.append(event)
        input_array = (Win32Input * len(events))(*events)
        sent = self.user32.SendInput(len(input_array), input_array, ctypes.sizeof(Win32Input))
        if sent != len(input_array):
            raise OSError(ctypes.get_last_error(), "Windows rejected the keyboard command.")

    def paste_filename_and_save(self, filename):
        if pyperclip is None:
            raise RuntimeError("The clipboard helper is unavailable. Run run_windows.bat to install requirements.")
        save_dialog = self.find_save_dialog()
        if save_dialog:
            self.user32.ShowWindow(save_dialog, self.SW_RESTORE)
            self.user32.SetForegroundWindow(save_dialog)
        try:
            previous_clipboard = pyperclip.paste()
        except Exception:
            previous_clipboard = None
        pyperclip.copy(filename)
        events = list(self._hotkey_events(self.VK_CONTROL, self.VK_A))
        events.extend(self._hotkey_events(self.VK_CONTROL, self.VK_V))
        events.extend(((self.VK_ENTER, 0), (self.VK_ENTER, 0x0002)))
        try:
            self._send_keyboard_events(events)
            time.sleep(0.2)
        finally:
            if previous_clipboard is not None:
                pyperclip.copy(previous_clipboard)

    def confirm_save(self):
        self._send_keyboard_events(((self.VK_ENTER, 0), (self.VK_ENTER, 0x0002)))

    def save_changes(self):
        self._send_keyboard_events(self._hotkey_events(self.VK_CONTROL, ord("S")))

    def find_save_dialog(self):
        found = {"hwnd": None}
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _):
            if not self.user32.IsWindowVisible(hwnd):
                return True
            title_length = self.user32.GetWindowTextLengthW(hwnd)
            if title_length <= 0:
                return True
            title = ctypes.create_unicode_buffer(title_length + 1)
            self.user32.GetWindowTextW(hwnd, title, len(title))
            if "save as" in title.value.lower():
                found["hwnd"] = hwnd
                return False
            return True

        callback = callback_type(visit)
        self.user32.EnumWindows(callback, 0)
        return found["hwnd"]

    def discard_save(self):
        events = ((self.VK_RIGHT, 0), (self.VK_RIGHT, 0x0002),
                  (self.VK_ENTER, 0), (self.VK_ENTER, 0x0002))
        self._send_keyboard_events(events)

    def cancel_save(self):
        self._send_keyboard_events(((self.VK_ESCAPE, 0), (self.VK_ESCAPE, 0x0002)))

    def is_open(self):
        return bool(self.hwnd and self.user32.IsWindow(self.hwnd))

    def close(self):
        hwnd = self._find_window() or self.hwnd
        if hwnd:
            self.user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0)


class ChromeController:
    """Launch Chrome and control the active Chrome window on Windows."""
    SW_RESTORE = 9
    WM_CLOSE = 0x0010
    KEYEVENTF_KEYUP = 0x0002
    INPUT_KEYBOARD = 1
    VK_CONTROL = 0x11
    VK_R = 0x52
    VK_LEFT = 0x25
    VK_RIGHT = 0x27
    VK_ALT = 0x12
    VK_F4 = 0x73
    VK_ENTER = 0x0D
    VK_BACK = 0x08
    VK_PRIOR = 0x21
    VK_NEXT = 0x22

    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("Chrome voice controls are supported on Windows only.")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.EnumWindows.argtypes = (
            ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
            wintypes.LPARAM,
        )
        self.user32.EnumWindows.restype = wintypes.BOOL
        self.user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.IsIconic.argtypes = (wintypes.HWND,)
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        self.user32.GetClassNameW.restype = ctypes.c_int
        self.user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        self.user32.BringWindowToTop.argtypes = (wintypes.HWND,)
        self.user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.PostMessageW.argtypes = (
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )
        self.user32.SendInput.argtypes = (
            wintypes.UINT, ctypes.POINTER(Win32Input), ctypes.c_int
        )
        self.user32.SendInput.restype = wintypes.UINT

    @staticmethod
    def executable_path():
        found = shutil.which("chrome.exe") or shutil.which("chrome")
        if found:
            return found
        install_roots = (
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        )
        for root in install_roots:
            if root:
                candidate = os.path.join(root, "Google", "Chrome", "Application", "chrome.exe")
                if os.path.isfile(candidate):
                    return candidate
        return None

    def open(self, url=None):
        executable = self.executable_path()
        if not executable:
            raise RuntimeError("Google Chrome wasn't found. Install Chrome, then try again.")
        args = [executable]
        if url:
            args.append(url)
        subprocess.Popen(args, close_fds=True)

    def find_window(self):
        found = {"hwnd": None}
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _):
            if not self.user32.IsWindowVisible(hwnd):
                return True
            class_name = ctypes.create_unicode_buffer(128)
            self.user32.GetClassNameW(hwnd, class_name, len(class_name))
            if class_name.value == "Chrome_WidgetWin_1":
                found["hwnd"] = hwnd
                return False
            return True

        callback = callback_type(visit)
        self.user32.EnumWindows(callback, 0)
        return found["hwnd"]

    def focus(self):
        hwnd = self.find_window()
        if not hwnd:
            raise RuntimeError("Chrome isn't open. Say ‘open Chrome’ first.")
        # SW_RESTORE also unmaximizes a maximized window. Restore only when
        # minimized so voice commands preserve Chrome's current window size.
        if self.user32.IsIconic(hwnd):
            self.user32.ShowWindow(hwnd, self.SW_RESTORE)
        self.user32.BringWindowToTop(hwnd)
        if (not self.user32.SetForegroundWindow(hwnd) and
                self.user32.GetForegroundWindow() != hwnd):
            raise RuntimeError("Windows would not focus Chrome. Click its window and try again.")
        time.sleep(0.05)
        return hwnd

    def shortcut(self, *keys):
        self.focus()
        self._send_key_chord(*keys)

    def shortcut_foreground(self, *keys):
        """Send a shortcut to the current app without switching to Chrome."""
        self._send_key_chord(*keys)

    def foreground_title(self):
        hwnd = self.user32.GetForegroundWindow()
        if not hwnd:
            return ""
        title_length = self.user32.GetWindowTextLengthW(hwnd)
        if title_length <= 0:
            return ""
        title = ctypes.create_unicode_buffer(title_length + 1)
        self.user32.GetWindowTextW(hwnd, title, len(title))
        return title.value

    def _send_key_chord(self, *keys):
        events = []
        for key in keys:
            event = Win32Input()
            event.type = self.INPUT_KEYBOARD
            event.ki = Win32KeyBdInput(key, 0, 0, 0, 0)
            events.append(event)
        for key in reversed(keys):
            event = Win32Input()
            event.type = self.INPUT_KEYBOARD
            event.ki = Win32KeyBdInput(key, 0, self.KEYEVENTF_KEYUP, 0, 0)
            events.append(event)
        input_array = (Win32Input * len(events))(*events)
        sent = self.user32.SendInput(len(input_array), input_array, ctypes.sizeof(Win32Input))
        if sent != len(input_array):
            raise OSError(ctypes.get_last_error(), "Windows rejected the Chrome keyboard command.")

    def type_text(self, text):
        if pyperclip is None:
            raise RuntimeError("Typing into Chrome needs the clipboard helper. Run run_windows.bat to install requirements.")
        self.focus()
        try:
            previous_clipboard = pyperclip.paste()
        except Exception:
            previous_clipboard = None
        pyperclip.copy(text)
        try:
            self.shortcut(self.VK_CONTROL, ord("V"))
            time.sleep(0.15)
        finally:
            if previous_clipboard is not None:
                pyperclip.copy(previous_clipboard)

    def delete_previous_words(self, count):
        if count < 1 or count > 100:
            raise ValueError("Choose between 1 and 100 words to delete.")
        self.focus()
        for _ in range(count):
            # Ctrl+Backspace removes one previous word in Chrome text inputs
            # and contenteditable fields without leaving a selection behind.
            self._send_key_chord(self.VK_CONTROL, self.VK_BACK)
            time.sleep(0.05)

    def close(self):
        hwnd = self.find_window()
        if not hwnd:
            return False
        self.user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0)
        return True

class WindowsAppController:
    """Resolve common Windows apps and Start Menu shortcuts by spoken name."""
    WM_CLOSE = 0x0010
    SW_MINIMIZE = 6
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    @staticmethod
    def normalize_name(value):
        return " ".join(re.sub(r"[^a-z0-9 ]+", " ", value.lower()).split())

    @classmethod
    def aliases(cls, target):
        normalized = cls.normalize_name(target)
        if normalized in ("files", "file", "file explorer", "explorer", "windows explorer"):
            return "explorer"
        if normalized in (
            "edge", "microsoft edge", "ed", "age", "eddge", "ed age",
            "microsoft ed", "microsoft age", "microsoft eddge", "microsoft ed age",
        ):
            return "edge"
        if normalized in ("store", "microsoft store", "windows store"):
            return "store"
        if normalized in ("settings", "windows settings", "system settings", "computer settings"):
            return "settings"
        return normalized

    @staticmethod
    def start_menu_roots():
        return tuple(path for path in (
            os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
            os.path.join(os.environ.get("PROGRAMDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
        ) if path and os.path.isdir(path))

    @classmethod
    def find_start_menu_shortcut(cls, target):
        normalized_target = cls.normalize_name(target)
        candidates = []
        for root in cls.start_menu_roots():
            for folder, _, filenames in os.walk(root):
                for filename in filenames:
                    if not filename.lower().endswith(".lnk"):
                        continue
                    stem = cls.normalize_name(os.path.splitext(filename)[0])
                    if stem == normalized_target:
                        return os.path.join(folder, filename)
                    if normalized_target in stem or stem in normalized_target:
                        candidates.append((len(stem), os.path.join(folder, filename)))
        if candidates:
            return sorted(candidates)[0][1]
        return None

    @classmethod
    def open_app(cls, target):
        if os.name != "nt":
            raise RuntimeError("Dynamic app commands are supported on Windows only.")
        alias = cls.aliases(target)
        if alias == "explorer":
            subprocess.Popen(["explorer.exe"], close_fds=True)
            return "File Explorer"
        if alias == "edge":
            executable = shutil.which("msedge.exe") or shutil.which("msedge")
            if not executable:
                for root in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES")):
                    if root:
                        candidate = os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe")
                        if os.path.isfile(candidate):
                            executable = candidate
                            break
            if executable:
                subprocess.Popen([executable], close_fds=True)
                return "Microsoft Edge"
        if alias == "store":
            os.startfile("ms-windows-store:")
            return "Microsoft Store"
        if alias == "settings":
            os.startfile("ms-settings:")
            return "Windows Settings"

        shortcut = cls.find_start_menu_shortcut(target)
        if shortcut:
            os.startfile(shortcut)
            return os.path.splitext(os.path.basename(shortcut))[0]
        executable = shutil.which(target)
        if executable:
            subprocess.Popen([executable], close_fds=True)
            return target
        raise RuntimeError(f"I couldn't find an installed app or Start Menu shortcut named {target}.")

    @classmethod
    def _process_image(cls, pid):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        )
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(cls.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            image_path = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, image_path, ctypes.byref(size)):
                return image_path.value
            return ""
        finally:
            kernel32.CloseHandle(handle)

    @classmethod
    def close_app(cls, target):
        if os.name != "nt":
            raise RuntimeError("Dynamic app commands are supported on Windows only.")
        alias = cls.aliases(target)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.EnumWindows.argtypes = (
            ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
            wintypes.LPARAM,
        )
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        matches = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            title_buffer = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(hwnd) + 1)
            user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
            title = cls.normalize_name(title_buffer.value)
            class_buffer = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
            class_name = class_buffer.value.lower()
            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            image_name = os.path.basename(cls._process_image(process_id.value)).lower()

            if alias == "explorer":
                matched = class_name in ("cabinetwclass", "explorewclass")
            elif alias == "edge":
                matched = image_name == "msedge.exe"
            elif alias == "store":
                matched = "microsoft store" in title or "windows store" in title
            else:
                matched = alias in title or alias in cls.normalize_name(image_name.removesuffix(".exe"))
            if matched:
                matches.append(hwnd)
            return True

        callback = callback_type(visit)
        user32.EnumWindows(callback, 0)
        for hwnd in matches:
            user32.PostMessageW(hwnd, cls.WM_CLOSE, 0, 0)
        return bool(matches)

    @classmethod
    def minimize_foreground_app(cls, fallback_hwnd=None):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)

        hwnd = user32.GetForegroundWindow()
        if hwnd:
            title_length = user32.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(title_length + 1)
            user32.GetWindowTextW(hwnd, title, len(title))
            if "desktop ai assistant" in title.value.lower() and fallback_hwnd:
                hwnd = fallback_hwnd
        if not hwnd:
            hwnd = fallback_hwnd
        if not hwnd:
            raise RuntimeError("I couldn't find an active application window to minimize.")
        user32.ShowWindow(hwnd, cls.SW_MINIMIZE)
        return True


class VoiceListener(QThread):
    command = Signal(str)
    status = Signal(str)
    wake_detected = Signal()

    def __init__(self):
        super().__init__()
        self.recognizer = sr.Recognizer()
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.keep_conversation_event = threading.Event()
        self.resume_conversation_event = threading.Event()
        self.wake_response_finished = threading.Event()

    def run(self):
        try:
            with SoundDeviceMicrophone() as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=0.7)
                self.status.emit("Listening for ‘Astra’…")
                conversation_active = False
                last_activity = 0.0
                while not self.stop_event.is_set():
                    if self.pause_event.is_set():
                        time.sleep(0.1)
                        continue
                    if self.resume_conversation_event.is_set():
                        conversation_active = True
                        last_activity = time.monotonic()
                        self.resume_conversation_event.clear()
                        self.status.emit("Conversation active. Go ahead; no need to repeat ‘Astra’.")
                    if (conversation_active and not self.keep_conversation_event.is_set() and
                            time.monotonic() - last_activity >= CONVERSATION_IDLE_TIMEOUT_SECONDS):
                        conversation_active = False
                        self.status.emit("Wake mode: say ‘Astra’ (or ‘Astro’), or ‘goodbye’ to exit.")
                    try:
                        notes_mode = self.keep_conversation_event.is_set()
                        self.recognizer.pause_threshold = 1.1 if notes_mode else 0.8
                        audio = self.recognizer.listen(
                            source,
                            timeout=1,
                            phrase_time_limit=18 if notes_mode else 8,
                        )
                    except sr.WaitTimeoutError:
                        continue
                    try:
                        recognized_text = self.recognizer.recognize_google(
                            audio, language=RECOGNITION_LANGUAGE
                        ).strip()
                        heard = recognized_text.lower()
                    except sr.UnknownValueError:
                        if conversation_active or self.keep_conversation_event.is_set():
                            self.status.emit("I didn’t catch that. Try speaking closer to the microphone.")
                        else:
                            self.status.emit("I didn’t catch that. Try speaking closer to the microphone.")
                        continue
                    except sr.RequestError:
                        self.status.emit("Speech recognition is unavailable. Check your internet connection.")
                        continue

                    if conversation_active:
                        last_activity = time.monotonic()
                        # Astra is the assistant's display name, not a command
                        # or wake word. Ignore it when ASR hears it by itself.
                        if re.sub(r"[^a-z0-9 ]+", " ", heard).strip() == "astra":
                            continue
                        notes_mode = self.keep_conversation_event.is_set()
                        command = recognized_text if notes_mode else remove_wake_word(heard)
                        if command:
                            self.command.emit(command)
                        continue
                    # Exit phrases stay available after conversation timeout.
                    if is_exit_command(heard):
                        self.command.emit(heard)
                        continue
                    wake_variant = find_wake_word(heard)
                    if not wake_variant:
                        continue

                    self.wake_response_finished.clear()
                    self.wake_detected.emit()
                    self.wake_response_finished.wait()
                    conversation_active = True
                    last_activity = time.monotonic()
                    self.status.emit("Conversation active. Go ahead; no need to repeat ‘Astra’.")
                    command = remove_wake_word(heard)
                    if command:
                        self.command.emit(command)
                        continue
                    try:
                        command_audio = self.recognizer.listen(source, timeout=5, phrase_time_limit=5)
                        command = self.recognizer.recognize_google(
                            command_audio, language=RECOGNITION_LANGUAGE
                        ).lower()
                        last_activity = time.monotonic()
                        self.command.emit(command)
                    except sr.WaitTimeoutError:
                        self.status.emit("I didn’t hear a command. Go ahead when you’re ready.")
                    except sr.UnknownValueError:
                        self.status.emit("I couldn’t understand that. Try again when you’re ready.")
                    except sr.RequestError:
                        self.status.emit("Voice recognition needs an internet connection.")
        except (OSError, AttributeError) as exc:
            self.status.emit(f"Microphone unavailable: {exc}")

    def stop(self):
        self.stop_event.set()

    def pause(self):
        self.pause_event.set()

    def resume(self):
        self.pause_event.clear()


class SoundDeviceMicrophone(sr.AudioSource):
    """SpeechRecognition audio source backed by sounddevice (no PyAudio build)."""
    def __init__(self, sample_rate=16000, chunk_size=1024):
        self.SAMPLE_RATE = sample_rate
        self.SAMPLE_WIDTH = 2  # signed 16-bit PCM
        self.sample_rate = sample_rate
        self.CHUNK = chunk_size
        self.stream = None
        self.audio_stream = None

    def __enter__(self):
        audio_stream = sd.RawInputStream(
            samplerate=self.sample_rate,
            blocksize=self.CHUNK,
            channels=1,
            dtype="int16",
        )
        audio_stream.start()
        self.audio_stream = audio_stream
        self.stream = self.Stream(audio_stream)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.audio_stream is not None:
            self.audio_stream.stop()
            self.audio_stream.close()
            self.stream = None
            self.audio_stream = None

    class Stream:
        def __init__(self, audio_stream):
            self.audio_stream = audio_stream

        def read(self, size):
            data, overflowed = self.audio_stream.read(size)
            return bytes(data)


class CharacterArt(QWidget):
    """An original, minimal holographic assistant emblem drawn with Qt."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(210, 260)
        self.blink = False
        self.speaking = False
        self.phase = 0
        self.speech_timer = QTimer(self)
        self.speech_timer.timeout.connect(self.animate_speech)
        self.speech_timer.start(120)
        self.blink_timer = QTimer(self)
        self.blink_timer.timeout.connect(self.toggle_blink)
        self.blink_timer.start(2800)

    def toggle_blink(self):
        self.blink = True
        self.update()
        QTimer.singleShot(140, self.open_eyes)

    def set_speaking(self, speaking):
        self.speaking = speaking
        if speaking:
            self.phase = 0
            self.speech_timer.setInterval(45)
        else:
            self.speech_timer.setInterval(120)
        if not self.speech_timer.isActive():
            self.speech_timer.start()
        self.update()

    def animate_speech(self):
        self.phase = (self.phase + 9) % 360
        self.update()

    def open_eyes(self):
        self.blink = False
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # subtle holographic glow
        p.setPen(Qt.PenStyle.NoPen)
        glow_alpha = 34 if self.phase % 120 < 60 else 20
        p.setBrush(QColor(0, 205, 255, glow_alpha))
        p.drawEllipse(29, 22, 152, 152)
        p.setBrush(QColor(0, 205, 255, 14))
        p.drawEllipse(42, 35, 126, 126)

        # geometric hologram rings and orbit marks
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor("#38d9ff"), 2))
        p.drawEllipse(40, 30, 130, 130)
        p.setPen(QPen(QColor("#177ea8"), 1))
        p.drawEllipse(50, 40, 110, 110)
        p.setPen(QPen(QColor("#43e4ff"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        first_arc = self.phase * 16
        second_arc = ((self.phase + 180) % 360) * 16
        arc_length = (90 if self.speaking else 48) * 16
        p.drawArc(34, 24, 142, 142, first_arc, arc_length)
        p.drawArc(34, 24, 142, 142, second_arc, arc_length)
        p.setPen(QPen(QColor("#43e4ff"), 2))
        p.drawLine(105, 18, 105, 29)
        p.drawLine(105, 161, 105, 172)
        p.drawLine(28, 95, 40, 95)
        p.drawLine(170, 95, 182, 95)

        # abstract original AI core
        p.setPen(QPen(QColor("#9bf4ff"), 2))
        p.setBrush(QColor("#0a2942"))
        p.drawRoundedRect(69, 61, 72, 68, 18, 18)
        p.setBrush(QColor("#12bfe6"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(84, 79, 13, 13)
        p.drawEllipse(113, 79, 13, 13)
        p.setPen(QPen(QColor("#71eeff"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if self.blink:
            p.drawLine(83, 86, 98, 86)
            p.drawLine(111, 86, 126, 86)
        else:
            p.drawLine(90, 96, 90, 108)
            p.drawLine(119, 96, 119, 108)
        p.drawLine(99, 115, 111, 115)

        # base waveform
        p.setPen(QPen(QColor("#28c9ec"), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(61, 197, 79, 197)
        p.drawLine(84, 197, 91, 184)
        p.drawLine(95, 184, 103, 209)
        p.drawLine(107, 209, 115, 190)
        p.drawLine(119, 197, 148, 197)


class Companion(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Astra | Desktop AI Assistant")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.drag_offset = None
        self.edge_playback = os.path.join(os.path.dirname(sys.executable), "edge-playback.exe")
        self.speech_worker = None
        self.speech_queue = []
        self.quit_after_speech = False
        self.waiting_for_wake_speech = False
        self.chrome_controller = None
        self.command_target_hwnd = None
        self.pending_chrome_type = False
        self.pending_chrome_delete = False
        self.notes_window = None
        self.notes_active = False
        self.notes_session_active = False
        self.notes_save_prompt = False
        self.notes_save_filename_pending = False
        self.notes_save_check_pending = False
        self.close_after_save_filename = False

        self.bubble = QLabel("Say ‘Astra’ followed by a command.")
        self.bubble.setWordWrap(True)
        self.bubble.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bubble.setStyleSheet("""
            QLabel {
                background: rgba(8, 22, 36, 238);
                color: #dffaff;
                border: 1px solid #187e9c;
                border-radius: 12px;
                padding: 9px 12px;
                font-size: 12px;
            }
        """)
        self.bubble.setFixedWidth(230)

        self.art = CharacterArt()
        self.art.setToolTip("Drag to move. Right-click for options.")
        self.art.mousePressEvent = self.character_clicked
        self.art.mouseMoveEvent = self.overlay_mouse_move
        self.art.mouseReleaseEvent = self.overlay_mouse_release
        self.bubble.mousePressEvent = self.overlay_mouse_press
        self.bubble.mouseMoveEvent = self.overlay_mouse_move
        self.bubble.mouseReleaseEvent = self.overlay_mouse_release

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        layout.addWidget(self.bubble)
        layout.addWidget(self.art, alignment=Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background: transparent;")
        self.setFixedWidth(250)
        self.setMinimumHeight(330)
        self.adjustSize()

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 30, screen.bottom() - self.height() - 35)

        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        menu = QMenu()
        show_action = QAction("Show companion", self)
        show_action.triggered.connect(self.show)
        quit_action = QAction("Exit", self)
        quit_action.triggered.connect(self.close)
        menu.addAction(show_action)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip("Astra Desktop AI Assistant")
        self.tray.show()

        self.listener = VoiceListener()
        self.listener.command.connect(self.handle_voice_command)
        self.listener.status.connect(self.set_status)
        self.listener.wake_detected.connect(self.on_wake_word)
        self.listener.start()
        self.hide()

    def on_wake_word(self):
        if self.chrome_controller is None:
            self.chrome_controller = ChromeController()
        self.command_target_hwnd = self.chrome_controller.user32.GetForegroundWindow()
        self.show()
        self.raise_()
        self.waiting_for_wake_speech = True
        self.greet()

    def set_status(self, text):
        self.bubble.setText(text)

    def handle_voice_command(self, command):
        self.set_status(f"Heard: {command}")
        normalized = command.strip().lower()

        if self.notes_save_prompt:
            if "select save" in normalized or normalized == "save":
                if self.notes_window:
                    self.notes_window.confirm_save()
                self.notes_save_prompt = False
                self.notes_save_check_pending = True
                self.close_after_save_filename = True
                self.say("Saving your notes.")
                QTimer.singleShot(800, self.check_save_notes_dialog)
            elif "don't save" in normalized or "dont save" in normalized or "discard" in normalized:
                if self.notes_window:
                    self.notes_window.discard_save()
                self.notes_save_prompt = False
                self.notes_window = None
                self.say("I closed Notepad without saving. I'm back in conversation mode.")
            elif "cancel" in normalized:
                if self.notes_window:
                    self.notes_window.cancel_save()
                self.notes_save_prompt = False
                self.close_after_save_filename = False
                self.say("I cancelled closing Notepad. Your document is still open.")
            else:
                self.say("Notepad is asking whether to save. Say ‘select save’, ‘don't save’, or ‘cancel save’.")
            return

        if self.notes_save_filename_pending:
            if "cancel" in normalized:
                if self.notes_window:
                    self.notes_window.cancel_save()
                self.notes_save_filename_pending = False
                self.close_after_save_filename = False
                self.say("I cancelled saving. Your document remains open in Notepad.")
                return
            filename = command.strip().strip(" .")
            try:
                if not filename or not self.notes_window:
                    raise RuntimeError("I didn't get a filename.")
                self.notes_window.paste_filename_and_save(filename)
                self.notes_save_filename_pending = False
                self.notes_save_prompt = False
                if self.close_after_save_filename:
                    self.notes_window = None
                self.close_after_save_filename = False
                self.say(f"Saving your notes as {filename}.")
            except Exception as exc:
                self.say(f"I couldn't save that filename: {exc}. Please say the filename again.")
            return

        if self.pending_chrome_type:
            self.pending_chrome_type = False
            if normalized in ("cancel", "cancel typing"):
                self.say("Chrome typing cancelled.")
                return
            try:
                if self.chrome_controller is None:
                    self.chrome_controller = ChromeController()
                self.chrome_controller.type_text(command.strip())
                self.say("Typed into the focused field.")
            except Exception as exc:
                self.say(f"I couldn't type into the focused field: {exc}")
            return

        if self.pending_chrome_delete:
            self.pending_chrome_delete = False
            if normalized in ("cancel", "cancel delete", "never mind"):
                self.say("Deletion cancelled.")
                return
            count = self.parse_delete_word_count(normalized)
            if count is None:
                count = len(re.findall(r"\b[\w’'-]+\b", command.strip()))
            try:
                if self.chrome_controller is None:
                    self.chrome_controller = ChromeController()
                self.chrome_controller.delete_previous_words(count)
                self.say(f"Deleted the last {count} word{'s' if count != 1 else ''} from the focused Chrome field.")
            except Exception as exc:
                self.say(f"I couldn't delete those words in Chrome: {exc}")
            return

        if "save notes" in normalized or "save note" in normalized:
            self.save_notes()
            return

        if "open notepad" in normalized:
            self.open_notepad()
            return

        if self.handle_chrome_command(command, normalized):
            return

        if self.handle_windows_app_command(command, normalized):
            return

        if self.notes_active:
            if is_stop_notes_command(normalized):
                self.notes_active = False
                self.say("Dictation stopped. Your notes stay open. Say ‘take notes’ to continue or ‘exit notes’ to return to conversation mode.")
                return
            if (is_exit_notes_command(normalized) or
                    (normalized == "exit" and self.notes_window is not None)):
                self.exit_notes()
                return
            if "take notes" in normalized or "start notes" in normalized:
                self.say("I'm already taking notes. Keep speaking, or say ‘stop notes’ to stop.")
                return
            self.write_note(command)
            return

        if self.notes_session_active:
            if is_stop_notes_command(normalized):
                self.say("Dictation is already stopped. Say ‘take notes’ to continue, or ‘exit notes’ to return to conversation mode.")
                return
            if "take notes" in normalized or "start notes" in normalized:
                self.notes_active = True
                self.listener.keep_conversation_event.set()
                self.say("Dictation resumed. Start speaking, or say ‘stop notes’ when you want to pause.")
                return
            if is_exit_notes_command(normalized):
                self.exit_notes()
                return
            self.say("Notes are paused. Say ‘take notes’ to continue, or ‘exit notes’ to return to conversation mode.")
            return

        if "take notes" in normalized or "start notes" in normalized:
            self.start_notes()
            return
        if is_stop_notes_command(normalized):
            self.say("Dictation is not active. Say ‘take notes’ to start typing.")
            return
        if (is_exit_notes_command(normalized) or
                (normalized == "exit" and self.notes_window is not None)):
            self.exit_notes()
            return

        if is_exit_command(command):
            self.quit_after_speech = True
            self.say("Goodbye! It was lovely helping you. See you next time!")
        elif "time" in command or "o'clock" in command:
            now = datetime.now().astimezone()
            self.say(f"It's {now.strftime('%I:%M %p').lstrip('0')}! Is there anything else I can help with?")
        elif any(word in command for word in ("hide", "go away", "dismiss")):
            self.hide()
            self.tray.showMessage("Astra", "Hidden. Say ‘Astra, show’ to bring me back.")
        elif any(word in command for word in ("show", "come back", "appear")):
            self.show()
            self.say("I’m here.")
        elif any(word in command for word in ("exit", "quit", "shut down", "close")):
            self.listener.stop()
            self.close()
        elif any(word in command for word in ("hello", "hi", "good morning", "good evening")):
            self.greet()
        else:
            self.reply_locally(command)

    def ensure_notepad_open(self):
        if self.notes_window:
            try:
                self.notes_window.focus()
                return
            except Exception:
                self.notes_window = None
        self.notes_window = NotesWindow()

    @staticmethod
    def parse_delete_word_count(text):
        text = text.strip().lower().strip(" .!?,")
        match = re.fullmatch(
            r"(?:delete\s+)?(?:last\s+)?([a-z0-9]+)(?:\s+words?)?",
            text,
        )
        if not match:
            return None
        token = match.group(1)
        if token.isdigit():
            return int(token)
        counts = {
            "one": 1, "won": 1, "wun": 1,
            "two": 2, "too": 2, "to": 2,
            "three": 3, "tree": 3,
            "four": 4, "for": 4, "fore": 4,
            "five": 5, "fife": 5,
            "six": 6, "seven": 7,
            "eight": 8, "ate": 8,
            "nine": 9, "ten": 10,
            "eleven": 11, "twelve": 12, "thirteen": 13,
            "fourteen": 14, "fifteen": 15, "sixteen": 16,
            "seventeen": 17, "eighteen": 18, "nineteen": 19,
            "twenty": 20,
        }
        return counts.get(token)

    def handle_chrome_command(self, command, normalized):
        normalized = normalized.strip(" .!?,")
        search_match = re.match(r"^search(?:\s+for)?\s+(.+?)\s*$", command.strip(), re.IGNORECASE)
        type_match = re.match(r"^type(?:\s+(.+))?$", command.strip(), re.IGNORECASE)
        delete_match = re.match(r"^delete\s+last\s+(.+?)\s+words?$", normalized)
        if normalized == "search":
            self.say("What would you like me to search for?")
            return True
        if not (normalized == "open chrome" or search_match or type_match or delete_match or
                normalized in ("delete", "delete word") or
                normalized in ("reload", "reload tab", "go back", "go forward", "exit chrome",
                               "new tab", "close tab", "scroll down", "scroll up",
                               "enter", "press enter", "submit", "delete", "delete word")):
            return False

        if normalized == "exit chrome":
            try:
                controller = self.chrome_controller or ChromeController()
                if controller.close():
                    self.say("Chrome is closed.")
                else:
                    self.say("Chrome isn't open.")
            except Exception as exc:
                self.say(f"I couldn't close Chrome: {exc}")
            return True

        try:
            if self.chrome_controller is None:
                self.chrome_controller = ChromeController()
            controller = self.chrome_controller
            if type_match:
                text = (type_match.group(1) or "").strip()
                if text:
                    controller.type_text(text)
                    self.say("Typed into the focused field.")
                else:
                    controller.focus()
                    self.pending_chrome_type = True
                    self.say("What should I type? Focus the Chrome text field first, then dictate the words. Say ‘cancel’ to stop.")
            elif normalized == "delete":
                controller.focus()
                self.pending_chrome_delete = True
                self.say("What should I delete from the end of the focused field? Say the words, or say ‘delete last n words’. Say ‘cancel’ to stop.")
            elif normalized == "delete word":
                controller.delete_previous_words(1)
                self.say("Deleted the last word from the focused Chrome field.")
            elif delete_match:
                count = self.parse_delete_word_count(delete_match.group(1))
                if count is None:
                    self.say("I couldn't understand how many words to delete. Try ‘delete last n words’.")
                else:
                    controller.delete_previous_words(count)
                    self.say(f"Deleted the last {count} word{'s' if count != 1 else ''} from the focused Chrome field.")
            elif normalized == "open chrome":
                controller.open()
                self.say("Opening Chrome.")
            elif search_match:
                query = search_match.group(1).strip()
                if not query:
                    self.say("What would you like me to search for?")
                else:
                    search_url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
                    controller.open(search_url)
                    self.say(f"Searching Chrome for {query}.")
            elif normalized in ("reload", "reload tab"):
                controller.shortcut(controller.VK_CONTROL, controller.VK_R)
                self.say("Reloading the Chrome tab.")
            elif normalized == "go back":
                controller.shortcut(controller.VK_ALT, controller.VK_LEFT)
                self.say("Going back in Chrome.")
            elif normalized == "go forward":
                controller.shortcut(controller.VK_ALT, controller.VK_RIGHT)
                self.say("Going forward in Chrome.")
            elif normalized == "new tab":
                active_title = controller.foreground_title().lower()
                new_tab_key = ord("N") if "notepad" in active_title else ord("T")
                controller.shortcut_foreground(controller.VK_CONTROL, new_tab_key)
                self.say("Opened a new tab in the active application.")
            elif normalized == "close tab":
                controller.shortcut_foreground(controller.VK_CONTROL, ord("W"))
                self.say("Closed the current tab in the active application.")
            elif normalized == "scroll down":
                controller.shortcut_foreground(controller.VK_NEXT)
                self.say("Scrolling down in the active application.")
            elif normalized == "scroll up":
                controller.shortcut_foreground(controller.VK_PRIOR)
                self.say("Scrolling up in the active application.")
            elif normalized in ("enter", "press enter", "submit"):
                controller.shortcut(controller.VK_ENTER)
                self.say("Submitted the focused Chrome field.")
            elif normalized in ("delete", "delete word"):
                controller.shortcut(controller.VK_CONTROL, controller.VK_BACK)
                self.say("Deleted the previous word in the focused Chrome field.")
        except Exception as exc:
            self.say(f"I couldn't complete that Chrome command: {exc}")
        return True

    def handle_windows_app_command(self, command, normalized):
        if normalized.strip(" .!?,") in (
            "minimize", "minimize window", "minimize current app", "minimize current application",
            "minimise", "minimise window", "minimise current app", "minimise current application",
        ):
            try:
                WindowsAppController.minimize_foreground_app(self.command_target_hwnd)
                self.say("Minimized the active application.")
            except Exception as exc:
                self.say(f"I couldn't minimize the active application: {exc}")
            return True
        match = re.match(r"^(open|launch|start|close|exit)\s+(.+?)\s*[.!?]*$", command.strip(), re.IGNORECASE)
        if not match:
            return False
        action, target = match.groups()
        target = target.strip()
        target_normalized = WindowsAppController.normalize_name(target)
        if target_normalized in ("astra", "assistant", "the assistant", "companion"):
            return False
        try:
            if action.lower() in ("open", "launch", "start"):
                app_name = WindowsAppController.open_app(target)
                self.say(f"Opening {app_name}.")
            else:
                closed = WindowsAppController.close_app(target)
                if closed:
                    self.say(f"Closing {target}.")
                else:
                    self.say(f"I couldn't find an open window for {target}.")
        except Exception as exc:
            self.say(str(exc))
        return True

    def open_notepad(self):
        try:
            self.ensure_notepad_open()
            self.say("Notepad is open. Say ‘take notes’ when you want me to start typing.")
        except Exception as exc:
            self.notes_window = None
            self.say(f"I couldn't open Notepad: {exc}")

    def start_notes(self):
        try:
            self.ensure_notepad_open()
            self.notes_active = True
            self.notes_session_active = True
            self.listener.keep_conversation_event.set()
            self.say("Dictation started. Speak naturally, then say ‘stop notes’ to pause or ‘exit notes’ to return to conversation mode.")
        except Exception as exc:
            self.notes_window = None
            self.notes_active = False
            self.notes_session_active = False
            self.say(f"I couldn't start Notepad dictation: {exc}")

    def save_notes(self):
        if not self.notes_window:
            self.say("Notepad is not open. Say ‘open notepad’ or ‘take notes’ first.")
            return
        try:
            self.notes_window.focus()
            self.notes_save_prompt = False
            self.notes_save_filename_pending = False
            self.notes_save_check_pending = True
            self.close_after_save_filename = False
            self.notes_window.save_changes()
            self.say("Saving your notes.")
            QTimer.singleShot(800, self.check_save_notes_dialog)
        except Exception as exc:
            self.notes_save_check_pending = False
            self.say(f"I couldn't save your notes: {exc}")

    def check_save_notes_dialog(self):
        if not self.notes_save_check_pending:
            return
        self.notes_save_check_pending = False
        if self.notes_window and self.notes_window.find_save_dialog():
            self.notes_save_filename_pending = True
            self.say("What filename should I use for your notes?")
            return
        if self.close_after_save_filename:
            self.notes_window = None
            self.close_after_save_filename = False
            self.say("Your notes were saved and Notepad is closed. I'm back in conversation mode.")
        else:
            self.say("Your notes have been saved. Notepad stays open.")

    def exit_notes(self):
        if self.notes_window:
            self.notes_save_prompt = True
            self.notes_window.close()
            self.leave_notes_mode()
            QTimer.singleShot(500, self.check_notes_close_prompt)
        else:
            self.leave_notes_mode()
            self.say("Notepad is not open. I'm back in conversation mode.")

    def check_notes_close_prompt(self):
        if not self.notes_save_prompt:
            return
        if self.notes_window and self.notes_window.is_open():
            self.say("Notepad is asking whether to save. Say ‘select save’ to choose a filename, ‘don't save’ to discard, or ‘cancel save’ to keep editing.")
        else:
            self.notes_save_prompt = False
            self.notes_window = None
            self.say("Notepad closed. I'm back in conversation mode.")

    def leave_notes_mode(self):
        self.notes_active = False
        self.notes_session_active = False
        self.listener.keep_conversation_event.clear()
        self.listener.resume_conversation_event.set()

    def write_note(self, text):
        if not self.notes_window:
            self.leave_notes_mode()
            self.say("Notepad is no longer available. Say ‘take notes’ to start again.")
            return
        try:
            self.notes_window.type_text(text.strip() + " ")
        except Exception as exc:
            self.leave_notes_mode()
            self.say(f"I stopped dictation because I couldn't type into Notepad: {exc}")

    def reply_locally(self, command):
        if "test" in command:
            self.say("I hear you clearly! I'm Astra, ready to help.")
        elif "date" in command or "day is it" in command:
            today = datetime.now().astimezone()
            self.say(f"It's {today.strftime('%A, %B %d')}. What else can I help with?")
        elif "how are you" in command or "how are things" in command:
            self.say("I'm doing wonderfully, thank you for asking! How are you?")
        elif any(word in command for word in ("thank you", "thanks", "thank you astra")):
            self.say("You're very welcome! I'm always happy to help.")
        elif "help" in command or "what can you do" in command:
            self.say("I can greet you, tell the time or date, hide or show myself, and say goodbye.")
        else:
            self.say(f"I heard you say, {command}. I can help with the time, date, greetings, and desktop controls. What would you like to try?")

    def say(self, text):
        if self.speech_worker is None:
            self.set_status(text)
            self.bubble.repaint()
        self.art.set_speaking(True)
        self.listener.pause()
        self.speech_queue.append(text)
        self.start_next_speech()

    def start_next_speech(self):
        if self.speech_worker is not None or not self.speech_queue:
            return
        text = self.speech_queue.pop(0)
        worker = SpeechWorker(text, self.edge_playback)
        worker.speech_started.connect(self.on_speech_started)
        worker.finished.connect(self.on_speech_finished)
        self.speech_worker = worker
        worker.start()

    def on_speech_started(self, text):
        self.set_status(text)
        self.art.set_speaking(True)
        self.bubble.repaint()

    def on_speech_finished(self):
        worker = self.speech_worker
        self.speech_worker = None
        self.art.set_speaking(False)
        if self.waiting_for_wake_speech:
            self.waiting_for_wake_speech = False
            if self.command_target_hwnd:
                self.chrome_controller.user32.SetForegroundWindow(self.command_target_hwnd)
            self.listener.wake_response_finished.set()
        if worker and not worker.success:
            self.set_status(f"Aria could not speak: {worker.error}")
            self.tray.showMessage("Aria voice unavailable", worker.error[:240])
        elif worker:
            self.set_status(worker.text)
        if self.speech_queue:
            self.start_next_speech()
        elif self.quit_after_speech:
            self.close()
            QApplication.quit()
        else:
            self.listener.resume()

    def greet(self):
        now = datetime.now().astimezone()
        hour = now.hour
        if 5 <= hour < 12:
            salutation = "Good morning"
        elif 12 <= hour < 17:
            salutation = "Good afternoon"
        elif 17 <= hour < 21:
            salutation = "Good evening"
        else:
            salutation = "Hello"
        current_time = now.strftime("%I:%M %p").lstrip("0")
        greeting = f"{salutation}! It’s {current_time}. Astra is here. How can I help you?"
        self.say(greeting)

    def character_clicked(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            menu = QMenu(self)
            top_action = QAction("Toggle always on top", self)
            top_action.triggered.connect(self.toggle_topmost)
            exit_action = QAction("Exit companion", self)
            exit_action.triggered.connect(self.close)
            menu.addAction(top_action)
            menu.addAction(exit_action)
            menu.exec(event.globalPosition().toPoint())
        elif event.button() == Qt.MouseButton.LeftButton:
            self.begin_drag(event)

    def overlay_mouse_press(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.begin_drag(event)

    def begin_drag(self, event):
        self.drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        event.accept()

    def move_overlay(self, event):
        if self.drag_offset is None or not event.buttons() & Qt.MouseButton.LeftButton:
            return
        new_position = event.globalPosition().toPoint() - self.drag_offset
        screen = QApplication.screenAt(event.globalPosition().toPoint()) or QApplication.primaryScreen()
        bounds = screen.availableGeometry()
        x = max(bounds.left(), min(new_position.x(), bounds.right() - self.width() + 1))
        y = max(bounds.top(), min(new_position.y(), bounds.bottom() - self.height() + 1))
        self.move(x, y)
        event.accept()

    def overlay_mouse_move(self, event):
        self.move_overlay(event)

    def overlay_mouse_release(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_offset = None
            event.accept()

    def toggle_topmost(self):
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint,
                           not bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))
        self.show()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        self.move_overlay(event)

    def mouseReleaseEvent(self, event):
        self.drag_offset = None

    def closeEvent(self, event):
        self.listener.stop()
        self.listener.wake_response_finished.set()
        self.listener.resume()
        self.listener.wait(1500)
        if self.speech_worker is not None:
            self.speech_worker.wait(5000)
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    companion = Companion()
    sys.exit(app.exec())
