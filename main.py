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

try:
    from pywinauto import Desktop as UIAutomationDesktop
except ImportError:
    UIAutomationDesktop = None

try:
    import webrtcvad
except ImportError:
    webrtcvad = None

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

try:
    import pyperclip
except ImportError:
    pyperclip = None

RECOGNITION_LANGUAGE = "en-IN"

CONVERSATION_IDLE_TIMEOUT_SECONDS = 30

FEMALE_TTS_VOICE = "en-US-AriaNeural"

VOICE_ACTIVITY_VAD_MODE = 2

VOICE_ACTIVITY_FRAME_MS = 20

VOICE_ACTIVITY_MIN_VOICED_FRAMES = 4

VOICE_ACTIVITY_MIN_RATIO = 0.08

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

EXIT_PHRASES = ("goodbye",)

def is_exit_command(text):
    normalized = " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())
    return normalized in EXIT_PHRASES

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

class VoiceCommandSequenceParser:
    """Split command sequences using registered command families, not command pairs."""
    def __init__(self):
        self._command_starters = []
        self._separators = re.compile(r"\s*,?\s+(?:and\s+then|then|and)\s+", re.IGNORECASE)
        # Keep the original connectors above and recognize common spoken
        # transitions as additional ways to chain commands.
        self._additional_separators = re.compile(
            r"\s*,?\s+(?:and\s+(?:after\s+that|afterwards|afterward|next|finally|subsequently)|"
            r"after\s+that|afterwards|afterward|next|also|followed\s+by|subsequently|finally|"
            r"plus|but|or|so|yet)\s+",
            re.IGNORECASE,
        )

    def register_command_starter(self, pattern):
        """Register the start pattern for a voice-command family added to Astra."""
        compiled = re.compile(pattern, re.IGNORECASE)
        if compiled.pattern not in {item.pattern for item in self._command_starters}:
            self._command_starters.append(compiled)

    def is_command_start(self, text):
        candidate = text.strip(" \t\r\n,.!?")
        return any(pattern.match(candidate) for pattern in self._command_starters)

    def split(self, command):
        original = command.strip(" \t\r\n,.!?")
        if not self.is_command_start(original):
            return [original]

        parts = []
        part_start = 0
        boundaries = []
        separator_matches = sorted(
            (match for pattern in (self._separators, self._additional_separators)
             for match in pattern.finditer(command)),
            key=lambda match: match.start(),
        )
        for separator in separator_matches:
            current_start = max(
                (boundary for boundary in boundaries if boundary < separator.start()), default=0
            )
            current_segment = command[current_start:separator.start()].strip(" \t\r\n,.!?")
            # Once a type command starts, treat its full remainder as dictated
            # text, even when it contains words that are also commands.
            if re.match(r"^type\b", current_segment, re.IGNORECASE):
                continue
            if self.is_command_start(command[separator.end():]):
                boundaries.append(separator.end())

        # Voice often omits connectors between actions, as in
        # "scroll down new tab". Split when the text before a recognized
        # starter is itself a complete command; a type command consumes its
        # remaining text literally.
        adjacent_candidates = sorted(
            {match.start() for starter in self._command_starters for match in starter.finditer(command)}
        )
        scan_start = 0
        for position in adjacent_candidates:
            prior_separator = max((boundary for boundary in boundaries if boundary < position), default=0)
            scan_start = max(scan_start, prior_separator)
            if position <= scan_start or any(abs(position - boundary) < 2 for boundary in boundaries):
                continue
            prefix = command[scan_start:position].strip(" \t\r\n,.!?")
            if re.match(r"^type\b", command[scan_start:].lstrip(), re.IGNORECASE):
                continue
            is_bare_action = re.fullmatch(
                r"(?:open|launch|start|close|exit|type|search(?:\s+for)?)\b", prefix, re.IGNORECASE
            )
            if self.is_command_start(prefix) and not is_bare_action:
                boundaries.append(position)
                scan_start = position
        type_boundaries = [
            boundary for boundary in boundaries
            if re.match(r"^type\b", command[boundary:].lstrip(), re.IGNORECASE)
        ]
        if re.match(r"^type\b", original, re.IGNORECASE):
            type_boundaries.append(0)
        if type_boundaries:
            type_start = min(type_boundaries)
            boundaries = [boundary for boundary in boundaries if boundary <= type_start]
        boundaries = sorted(set(boundaries))
        for boundary in boundaries:
            part = command[part_start:boundary].strip(" \t\r\n,.!?")
            if self.is_command_start(part):
                parts.append(part)
                part_start = boundary

        final_part = command[part_start:].strip(" \t\r\n,.!?")
        if not parts:
            return [original]
        if final_part:
            parts.append(final_part)
        return parts

VOICE_COMMAND_SEQUENCES = VoiceCommandSequenceParser()

def register_voice_command_starter(pattern):
    """Add a command family's opening pattern to compound-command parsing.

    Call this when adding a new command handler so it can be chained with
    existing commands using "and" or "then" without editing the parser.
    """
    VOICE_COMMAND_SEQUENCES.register_command_starter(pattern)

for _starter_pattern in (
    r"(?:open|launch|start|close)\b",
    r"exit\s+(?:chrome|notepad|notes)\b",
    r"(?:take|start|stop|exit|save)\s+notes\b",
    r"(?:type|search|reload|go\s+back|go\s+forward|new\s+tab|close\s+tab|"
    r"(?:switch\s+to\s+)?(?:next|previous|prev)\s+tab|"
    r"scroll\s+(?:up|down)|(?:hit|press)\s+(?:enter|return)|(?:enter|return)|delete|"
    r"select\s+all|select\s+everything|copy|cut|paste|pay\s*st|undo|redo)\b",
    r"(?:minimi[sz]e|maximi[sz]e|hide|show)\b",
    r"(?:what\s+time|tell\s+me\s+the\s+time|goodbye)\b",
):
    register_voice_command_starter(_starter_pattern)

def split_compound_commands(command):
    return VOICE_COMMAND_SEQUENCES.split(command)

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
                engine.say(self.text)
                self.speech_started.emit(self.text)
                engine.runAndWait()
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
        self.process = None
        self.hwnd = self._find_window()
        if not self.hwnd:
            self.process = subprocess.Popen(["notepad.exe"])
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
            process_id = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            is_notepad = "notepad" in title.value.lower()
            if not is_notepad:
                image_path = WindowsAppController._process_image(process_id.value)
                is_notepad = os.path.basename(image_path).lower() == "notepad.exe"
            if not is_notepad:
                return True
            if self.process and process_id.value == self.process.pid:
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
        hwnd = self._find_window()
        if hwnd:
            self.hwnd = hwnd
            return True
        return False

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
    VK_SHIFT = 0x10
    VK_TAB = 0x09
    VK_R = 0x52
    VK_LEFT = 0x25
    VK_RIGHT = 0x27
    VK_ALT = 0x12
    VK_F4 = 0x73
    VK_ENTER = 0x0D
    VK_BACK = 0x08
    VK_DELETE = 0x2E
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

    def shortcut_foreground(self, *keys, fallback_hwnd=None):
        """Send a shortcut to the current app, recovering focus from Astra if needed."""
        hwnd = self.user32.GetForegroundWindow()
        if hwnd and "desktop ai assistant" in self.foreground_title().lower():
            hwnd = fallback_hwnd
        if not hwnd:
            hwnd = fallback_hwnd
        if not hwnd:
            raise RuntimeError("I couldn't find the active application to receive that shortcut.")
        if self.user32.IsIconic(hwnd):
            self.user32.ShowWindow(hwnd, self.SW_RESTORE)
        self.user32.BringWindowToTop(hwnd)
        if (not self.user32.SetForegroundWindow(hwnd) and
                self.user32.GetForegroundWindow() != hwnd):
            raise RuntimeError("Windows would not focus the active application. Click its window and try again.")
        time.sleep(0.05)
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
        self.type_text_foreground(text)

    def type_text_foreground(self, text):
        """Paste into the currently focused application without switching to Chrome."""
        if pyperclip is None:
            raise RuntimeError("Typing needs the clipboard helper. Run run_windows.bat to install requirements.")
        try:
            previous_clipboard = pyperclip.paste()
        except Exception:
            previous_clipboard = None
        pyperclip.copy(text)
        try:
            self._send_key_chord(self.VK_CONTROL, ord("V"))
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

    WM_CLOSE = 0x0010

    SW_MINIMIZE = 6

    SW_MAXIMIZE = 3

    SW_RESTORE = 9

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    BROWSER_EXECUTABLES = {
        "chrome": ("chrome.exe", "chrome"),
        "edge": ("msedge.exe", "msedge"),
        "brave": ("brave.exe", "brave"),
        "firefox": ("firefox.exe", "firefox"),
        "opera": ("opera.exe", "opera"),
    }

    NATIVE_DESTINATIONS = {
        "recycle_bin": ("shell", "RecycleBinFolder", "Recycle Bin"),
        "control_panel": ("shell", "ControlPanelFolder", "Control Panel"),
        "this_pc": ("shell", "MyComputerFolder", "This PC"),
        "downloads": ("shell", "Downloads", "Downloads"),
        "documents": ("shell", "Personal", "Documents"),
        "pictures": ("shell", "My Pictures", "Pictures"),
        "music": ("shell", "My Music", "Music"),
        "videos": ("shell", "My Video", "Videos"),
        "task_manager": ("exe", "taskmgr.exe", "Task Manager"),
        "calculator": ("exe", "calc.exe", "Calculator"),
        "paint": ("exe", "mspaint.exe", "Paint"),
        "snipping_tool": ("exe", "snippingtool.exe", "Snipping Tool"),
    }

    APP_NAME_ALIASES = {
        "vs": "visual studio",
        "visual studio": "visual studio",
        "vs code": "visual studio code",
        "vscode": "visual studio code",
        "vsc": "visual studio code",
        "v s code": "visual studio code",
        "v s c": "visual studio code",
        "visual studio code": "visual studio code",
        "ms word": "word",
        "microsoft word": "word",
        "winword": "word",
        "ms excel": "excel",
        "microsoft excel": "excel",
        "ms powerpoint": "powerpoint",
        "ms ppt": "powerpoint",
        "ms power point": "powerpoint",
        "microsoft powerpoint": "powerpoint",
        "power point": "powerpoint",
        "ppt": "powerpoint",
        "ms outlook": "outlook",
        "microsoft outlook": "outlook",
        "ms onenote": "onenote",
        "microsoft onenote": "onenote",
        "ms teams": "teams",
        "microsoft teams": "teams",
        "vlc": "vlc media player",
        "obs": "obs studio",
    }

    APP_EXECUTABLES = {
        "visual studio": ("devenv.exe", "devenv"),
        "visual studio code": ("code.exe", "code"),
        "word": ("winword.exe", "winword"),
        "excel": ("excel.exe", "excel"),
        "powerpoint": ("powerpnt.exe", "powerpnt"),
        "outlook": ("outlook.exe", "outlook"),
        "onenote": ("onenote.exe", "onenote"),
        "vlc media player": ("vlc.exe", "vlc"),
        "obs studio": ("obs64.exe", "obs32.exe", "obs"),
    }

    @staticmethod
    def normalize_name(value):
        return " ".join(re.sub(r"[^a-z0-9 ]+", " ", value.lower()).split())

    @classmethod
    def is_confirmation_button_label(cls, value):
        label = cls.normalize_name(value)
        action_starts = (
            "save", "dont save", "don t save", "do not save", "dont close", "don t close",
            "do not close", "dont exit", "don t exit", "do not exit", "discard", "cancel",
            "close", "yes", "no", "exit", "quit", "end task", "force quit",
            "keep", "stay", "leave", "continue", "ok", "overwrite", "replace",
            "stop", "terminate", "restart", "apply",
        )
        return any(label == action or label.startswith(action + " ") for action in action_starts)

    @classmethod
    def is_confirmation_prompt_text(cls, value):
        text = cls.normalize_name(value)
        return bool(re.search(
            r"\bare you sure\b|\bdo you want\b|\bconfirm\b|\bconfirmation\b|"
            r"\bunsaved\b|\bchanges? will be lost\b|\bwill be lost\b|"
            r"\b(?:close|exit|quit) without saving\b|\bthis will close\b|\bclose (?:this|the|current) (?:app|application|window|file|document)\b|"
            r"\b(?:close|exit|quit) (?:the )?(?:app|application|window|file|document|workspace|project|session|tabs?)\b|"
            r"\bbefore (?:closing|close|exiting|exit|quitting|quit)\b|"
            r"\bclose all (?:tabs|windows|documents|files)\b|\byou have \d+ tabs? open\b|"
            r"\b(save|close|exit|quit|overwrite|replace|discard) (?:all )?(?:changes|files|windows|tabs|documents)\b|"
            r"\bend task\b|\bforce quit\b",
            text,
        ))

    @classmethod
    def aliases(cls, target):
        normalized = cls.normalize_name(target)
        if normalized in ("files", "file", "file explorer", "explorer", "windows explorer"):
            return "explorer"
        native_aliases = {
            "recycle bin": "recycle_bin", "recyclebin": "recycle_bin", "trash": "recycle_bin",
            "control panel": "control_panel", "this pc": "this_pc", "my computer": "this_pc",
            "computer": "this_pc", "downloads folder": "downloads", "download folder": "downloads",
            "documents folder": "documents", "my documents": "documents",
            "pictures folder": "pictures", "my pictures": "pictures",
            "music folder": "music", "my music": "music", "videos folder": "videos",
            "video folder": "videos", "my videos": "videos",
            "task manager": "task_manager", "taskmgr": "task_manager",
            "calculator": "calculator", "calc": "calculator", "paint": "paint",
            "snipping tool": "snipping_tool", "snippingtool": "snipping_tool",
        }
        if normalized in native_aliases:
            return native_aliases[normalized]
        if normalized in cls.APP_NAME_ALIASES:
            return cls.APP_NAME_ALIASES[normalized]
        if normalized in (
            "edge", "microsoft edge", "ed", "age", "eddge", "ed age",
            "microsoft ed", "microsoft age", "microsoft eddge", "microsoft ed age",
        ):
            return "edge"
        if normalized in ("store", "microsoft store", "windows store"):
            return "store"
        if normalized in ("settings", "windows settings", "system settings", "computer settings"):
            return "settings"
        if normalized in ("google chrome",):
            return "chrome"
        return normalized

    @classmethod
    def browser_name(cls, target):
        alias = cls.aliases(target)
        return alias if alias in cls.BROWSER_EXECUTABLES else None

    @classmethod
    def browser_executable_path(cls, browser):
        browser = cls.browser_name(browser) or browser
        if browser not in cls.BROWSER_EXECUTABLES:
            return None
        for executable_name in cls.BROWSER_EXECUTABLES[browser]:
            executable = shutil.which(executable_name)
            if executable:
                return executable
        roots = (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                 os.environ.get("LOCALAPPDATA"))
        install_paths = {
            "chrome": ("Google", "Chrome", "Application", "chrome.exe"),
            "edge": ("Microsoft", "Edge", "Application", "msedge.exe"),
            "brave": ("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            "firefox": ("Mozilla Firefox", "firefox.exe"),
            "opera": ("Programs", "Opera", "opera.exe"),
        }
        for root in roots:
            if root:
                candidate = os.path.join(root, *install_paths[browser])
                if os.path.isfile(candidate):
                    return candidate
        return None

    @classmethod
    def open_url_in_browser(cls, browser, url):
        executable = cls.browser_executable_path(browser)
        if not executable:
            raise RuntimeError(f"{browser.title()} wasn't found. Install it, then try again.")
        subprocess.Popen([executable, url], close_fds=True)

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
    def find_registered_app_executable(cls, target):
        """Resolve an installed Windows app registered through App Paths."""
        if os.name != "nt":
            return None
        try:
            import winreg
        except ImportError:
            return None
        executable_name = target if target.lower().endswith(".exe") else f"{target}.exe"
        key_path = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{executable_name}"
        roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
        for root in roots:
            try:
                with winreg.OpenKey(root, key_path) as app_key:
                    executable, _ = winreg.QueryValueEx(app_key, None)
                executable = os.path.expandvars(executable.strip('"'))
                if os.path.isfile(executable):
                    return executable
            except OSError:
                continue
        return None

    @classmethod
    def open_app(cls, target):
        if os.name != "nt":
            raise RuntimeError("Dynamic app commands are supported on Windows only.")
        alias = cls.aliases(target)
        browser = cls.browser_name(target)
        if browser:
            executable = cls.browser_executable_path(browser)
            if executable:
                subprocess.Popen([executable], close_fds=True)
                return {"chrome": "Google Chrome", "edge": "Microsoft Edge",
                        "brave": "Brave", "firefox": "Firefox", "opera": "Opera"}[browser]
            raise RuntimeError(f"{browser.title()} wasn't found. Install it, then try again.")
        native_target = cls.NATIVE_DESTINATIONS.get(alias)
        if native_target:
            destination_type, destination, app_name = native_target
            if destination_type == "shell":
                subprocess.Popen(["explorer.exe", f"shell:{destination}"], close_fds=True)
            else:
                executable = shutil.which(destination) or cls.find_registered_app_executable(destination)
                if not executable:
                    raise RuntimeError(f"{app_name} wasn't found on this computer.")
                subprocess.Popen([executable], close_fds=True)
            return app_name
        if alias == "explorer":
            subprocess.Popen(["explorer.exe"], close_fds=True)
            return "File Explorer"
        if alias == "edge":
            executable = cls.edge_executable_path()
            if executable:
                subprocess.Popen([executable], close_fds=True)
                return "Microsoft Edge"
        if alias == "store":
            os.startfile("ms-windows-store:")
            return "Microsoft Store"
        if alias == "settings":
            os.startfile("ms-settings:")
            return "Windows Settings"

        lookup_name = alias or target
        shortcut = cls.find_start_menu_shortcut(lookup_name)
        if shortcut:
            os.startfile(shortcut)
            return os.path.splitext(os.path.basename(shortcut))[0]
        executable = None
        executable_names = cls.APP_EXECUTABLES.get(lookup_name, (lookup_name, target))
        for executable_name in executable_names:
            executable = shutil.which(executable_name)
            if not executable:
                executable = cls.find_registered_app_executable(executable_name)
            if executable:
                break
        if executable:
            subprocess.Popen([executable], close_fds=True)
            return lookup_name.title()
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
    def foreground_browser(cls, fallback_hwnd=None):
        """Return the browser owning the foreground window, when recognized."""
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD

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
            return None

        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        image_name = os.path.basename(cls._process_image(process_id.value)).lower()
        for browser, executable_names in cls.BROWSER_EXECUTABLES.items():
            if image_name in executable_names:
                return browser
        return None

    @classmethod
    def edge_executable_path(cls):
        executable = shutil.which("msedge.exe") or shutil.which("msedge")
        if executable:
            return executable
        for root in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES")):
            if root:
                candidate = os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe")
                if os.path.isfile(candidate):
                    return candidate
        return None

    @classmethod
    def open_url_in_edge(cls, url):
        executable = cls.edge_executable_path()
        if not executable:
            raise RuntimeError("Microsoft Edge wasn't found. Install Edge, then try again.")
        subprocess.Popen([executable, url], close_fds=True)

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
        process_ids = set()
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
                process_ids.add(process_id.value)
            return True

        callback = callback_type(visit)
        user32.EnumWindows(callback, 0)
        for hwnd in matches:
            user32.PostMessageW(hwnd, cls.WM_CLOSE, 0, 0)
        if not matches:
            return None
        return {"hwnds": [int(hwnd) for hwnd in matches], "pids": sorted(process_ids)}

    @classmethod
    def find_close_confirmation(cls, process_ids, excluded_hwnds=()):
        """Find a visible confirmation dialog belonging to an app being closed."""
        if os.name != "nt" or not process_ids:
            return None
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.EnumWindows.argtypes = (
            ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM,
        )
        user32.EnumWindows.restype = wintypes.BOOL
        user32.EnumChildWindows.argtypes = (
            wintypes.HWND, ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
            wintypes.LPARAM,
        )
        user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)

        excluded = {int(hwnd) for hwnd in excluded_hwnds}
        candidates = []
        enum_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def read_text(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, len(buffer))
            return buffer.value.strip()

        def visit_window(hwnd, _):
            if not user32.IsWindowVisible(hwnd) or int(hwnd) in excluded:
                return True
            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if process_id.value not in process_ids:
                return True

            class_buffer = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
            child_texts = []
            buttons = []

            def visit_child(child_hwnd, _child_data):
                child_text = read_text(child_hwnd)
                if child_text:
                    child_texts.append(child_text)
                    child_class = ctypes.create_unicode_buffer(128)
                    user32.GetClassNameW(child_hwnd, child_class, len(child_class))
                    if child_class.value.lower() == "button":
                        buttons.append({"hwnd": int(child_hwnd), "label": child_text})
                return True

            child_callback = enum_type(visit_child)
            user32.EnumChildWindows(hwnd, child_callback, 0)
            options = [
                button for button in buttons
                if cls.is_confirmation_button_label(button["label"])
            ]
            is_standard_dialog = class_buffer.value == "#32770"
            prompt_text = " ".join(
                text for text in child_texts
                if cls.normalize_name(text) not in {
                    cls.normalize_name(button["label"]) for button in buttons
                }
            )
            looks_like_prompt = cls.is_confirmation_prompt_text(prompt_text)
            if options and (is_standard_dialog or looks_like_prompt or len(options) >= 2):
                candidates.append({
                    "hwnd": int(hwnd),
                    "title": read_text(hwnd),
                    "text": prompt_text,
                    "buttons": options,
                })
            return True

        callback = enum_type(visit_window)
        user32.EnumWindows(callback, 0)
        return candidates[0] if candidates else cls.find_uia_close_confirmation(process_ids)

    @classmethod
    def find_uia_close_confirmation(cls, process_ids):
        """Read confirmation text and buttons from an app's UI Automation tree."""
        if UIAutomationDesktop is None:
            return None
        try:
            windows = UIAutomationDesktop(backend="uia").windows()
        except Exception:
            return None

        for window in windows:
            try:
                if window.element_info.process_id not in process_ids:
                    continue
                title = window.window_text().strip()
                window_rect = window.rectangle()
                controls = []
                for control in window.descendants():
                    try:
                        info = control.element_info
                        name = (info.name or control.window_text() or "").strip()
                        rect = info.rectangle
                        bounds = (rect.left, rect.top, rect.right, rect.bottom)
                        if name and bounds[2] > bounds[0] and bounds[3] > bounds[1]:
                            controls.append({
                                "name": name,
                                "normalized": cls.normalize_name(name),
                                "type": info.control_type,
                                "bounds": bounds,
                                "control": control,
                            })
                    except Exception:
                        continue

                markers = [
                    item for item in controls
                    if item["type"] != "Button" and cls.is_confirmation_prompt_text(item["name"])
                ]
                if title and cls.is_confirmation_prompt_text(title):
                    markers.append({
                        "name": title,
                        "normalized": cls.normalize_name(title),
                        "type": "Window",
                        "bounds": (window_rect.left, window_rect.top, window_rect.right, window_rect.bottom),
                    })
                markers.sort(key=lambda item: (
                    (item["bounds"][2] - item["bounds"][0]) *
                    (item["bounds"][3] - item["bounds"][1])
                ))
                for marker in markers:
                    left, top, right, bottom = marker["bounds"]
                    # Scope choices to the area around the confirmation text. This
                    # avoids reading or selecting unrelated toolbar/page controls.
                    region = (left - 100, top - 45, right + 420, bottom + 360)

                    def in_prompt_region(item):
                        item_left, item_top, item_right, item_bottom = item["bounds"]
                        center_x = (item_left + item_right) / 2
                        center_y = (item_top + item_bottom) / 2
                        return (region[0] <= center_x <= region[2] and
                                region[1] <= center_y <= region[3])

                    prompt_controls = [item for item in controls if in_prompt_region(item)]
                    options = []
                    seen_options = set()
                    for item in prompt_controls:
                        if item["type"] != "Button" or not cls.is_confirmation_button_label(item["name"]):
                            continue
                        key = (item["normalized"], item["bounds"])
                        if key in seen_options:
                            continue
                        seen_options.add(key)
                        options.append({
                            "hwnd": 0,
                            "label": item["name"],
                            "uia_control": item["control"],
                        })
                    if not options:
                        continue

                    relevant_text = [marker["name"]]
                    for item in prompt_controls:
                        normalized = item["normalized"]
                        is_context = (
                            cls.is_confirmation_prompt_text(item["name"]) or
                            re.search(r"don.?t ask me again|tabs? open|changes? will be lost", normalized)
                        )
                        if item["type"] != "Button" and is_context:
                            if item["name"] not in relevant_text:
                                relevant_text.append(item["name"])
                    return {
                        "hwnd": int(window.handle),
                        "title": title,
                        "text": " ".join(relevant_text),
                        "buttons": options,
                        "uia": True,
                    }
            except Exception:
                continue
        return None

    @classmethod
    def find_editable_fields(cls, target_hwnd):
        """Return visible, editable controls in the active app, including focused state."""
        if UIAutomationDesktop is None or not target_hwnd or os.name != "nt":
            return []
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetWindowThreadProcessId.argtypes = (
                wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
            )
            target_process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(
                wintypes.HWND(int(target_hwnd)), ctypes.byref(target_process_id)
            )
            desktop_windows = UIAutomationDesktop(backend="uia").windows()
        except Exception:
            return []

        matching_windows = []
        exact_windows = []
        process_windows = []
        for window in desktop_windows:
            try:
                if int(window.handle) == int(target_hwnd):
                    exact_windows.append(window)
                elif getattr(window.element_info, "process_id", None) == target_process_id.value:
                    process_windows.append(window)
            except Exception:
                continue
        # Scope Chrome to the active top-level window. Its process owns other
        # windows too, so scanning every same-process root creates duplicate bars.
        matching_windows = exact_windows or process_windows

        fields = []
        seen_bounds = {}
        for window in matching_windows:
            try:
                for control in window.descendants():
                    try:
                        info = control.element_info
                        control_type = info.control_type
                        if control_type not in ("Edit", "ComboBox", "Document"):
                            continue
                        if not control.is_visible() or not control.is_enabled():
                            continue
                        rect = info.rectangle
                        bounds = (rect.left, rect.top, rect.right, rect.bottom)
                        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                            continue
                        focused = bool(control.has_keyboard_focus())
                        if control_type == "Document" and not focused:
                            continue
                        name = (info.name or control.window_text() or "").strip()
                        automation_id = (getattr(info, "auto_id", "") or "").strip()
                        label = name or automation_id or "text field"
                        search_context = cls.normalize_name(f"{name} {automation_id}")
                        search_words = set(search_context.split())
                        is_address_bar = bool(search_words & {"address", "url", "omnibox"})
                        search_score = 0 if is_address_bar else sum(
                            term in search_words
                            for term in ("search", "find", "query", "filter")
                        )
                        field = {
                            "label": label,
                            "automation_id": automation_id,
                            "control_type": control_type,
                            "focused": focused,
                            "search_score": search_score,
                            "is_address_bar": is_address_bar,
                            "bounds": bounds,
                            "control": control,
                        }
                        bounds_key = bounds
                        existing_index = seen_bounds.get(bounds_key)
                        if existing_index is None:
                            seen_bounds[bounds_key] = len(fields)
                            fields.append(field)
                        elif (focused and not fields[existing_index]["focused"]) or (
                                control_type == "Edit" and
                                fields[existing_index]["control_type"] == "ComboBox"):
                            fields[existing_index] = field
                    except Exception:
                        continue
            except Exception:
                continue
        # Some browser UIA trees expose several wrappers for the same omnibox.
        # In one active window, treat all of those wrappers as one logical field.
        deduplicated = []
        address_bar_index = None
        for field in fields:
            if not field["is_address_bar"]:
                deduplicated.append(field)
                continue
            if address_bar_index is None:
                address_bar_index = len(deduplicated)
                deduplicated.append(field)
                continue
            current = deduplicated[address_bar_index]
            rank = lambda item: (
                item["focused"], item["control_type"] == "Edit",
                -item["bounds"][1], -item["bounds"][0],
            )
            if rank(field) > rank(current):
                deduplicated[address_bar_index] = field
        return deduplicated

    @staticmethod
    def choose_close_confirmation(dialog, spoken_choice):
        """Click the requested visible dialog button, returning its label or None."""
        if os.name != "nt" or not dialog:
            return None
        normalized = WindowsAppController.normalize_name(spoken_choice)
        spoken_option = re.sub(
            r"^(?:please\s+)?(?:choose|select|click|press|pick)\s+(?:(?:the|option|button)\s+)*",
            "",
            normalized,
        )
        choice_map = (
            (("dont save", "don t save", "do not save", "discard", "no save"),
             ("dont save", "don t save", "do not save", "discard")),
            (("save all",), ("save all",)),
            (("save",), ("save",)),
            (("close all",), ("close all",)),
            (("cancel", "keep open", "stop closing", "never mind", "stay", "not now"),
             ("cancel", "keep open", "cancel close", "cancel closing", "stay")),
            (("yes",), ("yes",)),
            (("no",), ("no",)),
            (("overwrite", "replace"), ("overwrite", "replace")),
            (("exit", "quit", "close", "continue", "end task", "force quit", "leave"),
             ("exit", "quit", "close", "continue", "end task", "force quit", "leave")),
            (("ok", "okay"), ("ok", "okay")),
        )
        desired = None
        for spoken_aliases, button_aliases in choice_map:
            if any(re.search(rf"\b{re.escape(alias)}\b", spoken_option) for alias in spoken_aliases):
                desired = button_aliases
                break
        ordinal_match = re.search(r"\b(first|second|third|1st|2nd|3rd)\s+option\b", normalized)
        if ordinal_match:
            index = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2}[ordinal_match.group(1)]
            if index < len(dialog["buttons"]):
                button = dialog["buttons"][index]
                desired = (WindowsAppController.normalize_name(button["label"]),)
        if desired is None:
            matching_labels = tuple(
                WindowsAppController.normalize_name(button["label"])
                for button in dialog["buttons"]
                if (WindowsAppController.normalize_name(button["label"]) == spoken_option or
                    re.search(
                        rf"\b{re.escape(WindowsAppController.normalize_name(button['label']))}\b",
                        spoken_option,
                    ))
            )
            desired = matching_labels or (spoken_option,)

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        # Prefer the exact button so saying "save" cannot accidentally choose "Save All".
        for button in dialog["buttons"]:
            label = WindowsAppController.normalize_name(button["label"])
            if label in desired:
                if button.get("uia_control") is not None:
                    try:
                        button["uia_control"].click_input()
                        return button["label"]
                    except Exception:
                        continue
                hwnd = wintypes.HWND(button["hwnd"])
                if user32.IsWindow(hwnd):
                    user32.SendMessageW(hwnd, 0x00F5, 0, 0)  # BM_CLICK
                    return button["label"]
        for button in dialog["buttons"]:
            label = WindowsAppController.normalize_name(button["label"])
            if any(alias not in ("save", "yes", "no") and label.startswith(alias + " ")
                   for alias in desired):
                if button.get("uia_control") is not None:
                    try:
                        button["uia_control"].click_input()
                        return button["label"]
                    except Exception:
                        continue
                hwnd = wintypes.HWND(button["hwnd"])
                if user32.IsWindow(hwnd):
                    user32.SendMessageW(hwnd, 0x00F5, 0, 0)  # BM_CLICK
                    return button["label"]
        return None

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

    @classmethod
    def maximize_app(cls, target):
        """Maximize a matching open app window and report its previous state."""
        if os.name != "nt":
            raise RuntimeError("Window commands are supported on Windows only.")
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
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.IsIconic.argtypes = (wintypes.HWND,)
        user32.IsIconic.restype = wintypes.BOOL
        user32.IsZoomed.argtypes = (wintypes.HWND,)
        user32.IsZoomed.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.BringWindowToTop.argtypes = (wintypes.HWND,)
        user32.SetForegroundWindow.argtypes = (wintypes.HWND,)

        matches = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            title_buffer = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(hwnd) + 1)
            user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
            class_buffer = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            image_name = os.path.basename(cls._process_image(process_id.value)).lower()
            if alias == "explorer":
                matched = class_buffer.value.lower() in ("cabinetwclass", "explorewclass")
            elif alias == "edge":
                matched = image_name == "msedge.exe"
            elif alias == "store":
                title = cls.normalize_name(title_buffer.value)
                matched = "microsoft store" in title or "windows store" in title
            else:
                matched = (alias in cls.normalize_name(title_buffer.value) or
                           alias in cls.normalize_name(image_name.removesuffix(".exe")))
            if matched:
                matches.append(hwnd)
            return True

        callback = callback_type(visit)
        user32.EnumWindows(callback, 0)
        if not matches:
            return None

        hwnd = matches[0]
        if user32.IsZoomed(hwnd):
            return "already_maximized"
        was_minimized = bool(user32.IsIconic(hwnd))
        if was_minimized:
            user32.ShowWindow(hwnd, cls.SW_RESTORE)
        user32.ShowWindow(hwnd, cls.SW_MAXIMIZE)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        return "maximized_from_minimized" if was_minimized else "maximized"

    @classmethod
    def minimize_app(cls, target):
        """Minimize a matching open app window, or report its current state."""
        if os.name != "nt":
            raise RuntimeError("Window commands are supported on Windows only.")
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
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.IsIconic.argtypes = (wintypes.HWND,)
        user32.IsIconic.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)

        matches = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            title_buffer = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(hwnd) + 1)
            user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
            class_buffer = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            image_name = os.path.basename(cls._process_image(process_id.value)).lower()
            if alias == "explorer":
                matched = class_buffer.value.lower() in ("cabinetwclass", "explorewclass")
            elif alias == "edge":
                matched = image_name == "msedge.exe"
            elif alias == "store":
                title = cls.normalize_name(title_buffer.value)
                matched = "microsoft store" in title or "windows store" in title
            else:
                matched = (alias in cls.normalize_name(title_buffer.value) or
                           alias in cls.normalize_name(image_name.removesuffix(".exe")))
            if matched:
                matches.append(hwnd)
            return True

        callback = callback_type(visit)
        user32.EnumWindows(callback, 0)
        if not matches:
            return None

        hwnd = matches[0]
        if user32.IsIconic(hwnd):
            return "already_minimized"
        user32.ShowWindow(hwnd, cls.SW_MINIMIZE)
        return "minimized"

    @classmethod
    def minimize_all_apps(cls):
        """Minimize visible application windows while keeping Astra available."""
        if os.name != "nt":
            raise RuntimeError("Window commands are supported on Windows only.")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.EnumWindows.argtypes = (
            ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
            wintypes.LPARAM,
        )
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.IsIconic.argtypes = (wintypes.HWND,)
        user32.IsIconic.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)

        counts = {"minimized": 0, "already_minimized": 0}
        excluded_classes = {"progman", "workerw", "shell_traywnd", "dv2controlhost"}
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            title_length = user32.GetWindowTextLengthW(hwnd)
            if title_length == 0:
                return True
            title_buffer = ctypes.create_unicode_buffer(title_length + 1)
            user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
            if "desktop ai assistant" in title_buffer.value.lower():
                return True
            class_buffer = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
            if class_buffer.value.lower() in excluded_classes:
                return True
            if user32.IsIconic(hwnd):
                counts["already_minimized"] += 1
            else:
                user32.ShowWindow(hwnd, cls.SW_MINIMIZE)
                counts["minimized"] += 1
            return True

        callback = callback_type(visit)
        user32.EnumWindows(callback, 0)
        return counts

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
        self.hold_conversation_event = threading.Event()
        self.resume_conversation_event = threading.Event()
        self.wake_response_finished = threading.Event()
        self.vad = webrtcvad.Vad(VOICE_ACTIVITY_VAD_MODE) if webrtcvad is not None else None

    def contains_human_voice(self, audio):
        """Reject clips without a sustained speech-like signal before cloud ASR."""
        if self.vad is None:
            return True
        try:
            raw_audio = audio.get_raw_data(convert_rate=16000, convert_width=2)
            frame_size = int(16000 * VOICE_ACTIVITY_FRAME_MS / 1000) * 2
            voiced_frames = 0
            longest_voiced_run = 0
            current_voiced_run = 0
            total_frames = len(raw_audio) // frame_size
            for frame_index in range(total_frames):
                start = frame_index * frame_size
                frame = raw_audio[start:start + frame_size]
                if self.vad.is_speech(frame, 16000):
                    voiced_frames += 1
                    current_voiced_run += 1
                    longest_voiced_run = max(longest_voiced_run, current_voiced_run)
                else:
                    current_voiced_run = 0
            if not total_frames:
                return False
            required_voiced_frames = max(
                VOICE_ACTIVITY_MIN_VOICED_FRAMES,
                int(total_frames * VOICE_ACTIVITY_MIN_RATIO + 0.999),
            )
            return (voiced_frames >= required_voiced_frames and longest_voiced_run >= 3)
        except Exception:
            # A malformed or unsupported clip should not crash the microphone loop.
            return True

    def run(self):
        try:
            with SoundDeviceMicrophone() as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=0.7)
                if self.vad is None:
                    self.status.emit("Voice filter unavailable; install requirements.txt. Listening for ‘Astra’…")
                else:
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
                            not self.hold_conversation_event.is_set() and
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
                    if not self.contains_human_voice(audio):
                        continue
                    try:
                        recognized_text = self.recognizer.recognize_google(
                            audio, language=RECOGNITION_LANGUAGE
                        ).strip()
                        heard = recognized_text.lower()
                    except sr.UnknownValueError:
                        if (conversation_active and not self.keep_conversation_event.is_set() and
                                not self.hold_conversation_event.is_set() and
                                time.monotonic() - last_activity >= CONVERSATION_IDLE_TIMEOUT_SECONDS):
                            conversation_active = False
                            self.status.emit("Wake mode: say ‘Astra’ (or ‘Astro’), or ‘goodbye’ to exit.")
                        elif conversation_active or self.keep_conversation_event.is_set():
                            self.status.emit("I didn’t catch that. Try speaking closer to the microphone.")
                        continue
                    except sr.RequestError:
                        self.status.emit("Speech recognition is unavailable. Check your internet connection.")
                        continue

                    # A listen may start before the idle deadline and finish after it.
                    # Recheck here so a wake word captured at that boundary is handled
                    # in wake mode instead of being discarded as a conversational reply.
                    if (conversation_active and not self.keep_conversation_event.is_set() and
                            not self.hold_conversation_event.is_set() and
                            time.monotonic() - last_activity >= CONVERSATION_IDLE_TIMEOUT_SECONDS):
                        conversation_active = False
                        self.status.emit("Wake mode: say ‘Astra’ (or ‘Astro’), or ‘goodbye’ to exit.")

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
                        if not self.contains_human_voice(command_audio):
                            continue
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
        self.last_opened_browser = None
        self.last_typed_text = ""
        self.select_all_pending_delete = False
        self.command_target_hwnd = None
        self.pending_chrome_type = False
        self.pending_type_text = None
        self.pending_type_fields = []
        self.pending_type_attempts = 0
        self.pending_chrome_delete = False
        self.notes_window = None
        self.notes_active = False
        self.notes_session_active = False
        self.notes_save_prompt = False
        self.notes_save_filename_pending = False
        self.notes_save_check_pending = False
        self.close_after_save_filename = False
        self.pending_app_close_watch = None
        self.pending_app_close_confirmation = None

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
        if self.chrome_controller is not None:
            foreground_hwnd = self.chrome_controller.user32.GetForegroundWindow()
            if (foreground_hwnd and
                    "desktop ai assistant" not in self.chrome_controller.foreground_title().lower()):
                self.command_target_hwnd = foreground_hwnd

        if self.pending_app_close_confirmation and not is_exit_command(command):
            if self.handle_app_close_confirmation_reply(command):
                return
        elif is_exit_command(command):
            self.pending_app_close_confirmation = None
            self.pending_app_close_watch = None
            self.listener.hold_conversation_event.clear()

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

        if (is_exit_notes_command(normalized) or normalized in ("close notes", "close notepad")) and (
                self.notes_session_active or self.notes_window is not None):
            self.exit_notes()
            return

        if self.pending_type_text is not None:
            if normalized in ("cancel", "cancel typing", "never mind"):
                self.pending_type_text = None
                self.pending_type_fields = []
                self.pending_type_attempts = 0
                self.say("Field selection cancelled.")
                return

            selected_field = self.select_pending_type_field(command)
            if selected_field is None:
                choices = ", ".join(
                    f"{field['label']} (say {index})"
                    for index, field in enumerate(self.pending_type_fields, start=1)
                )
                if VOICE_COMMAND_SEQUENCES.is_command_start(command):
                    # A new command cancels the pending choice and continues normally.
                    self.pending_type_text = None
                    self.pending_type_fields = []
                    self.pending_type_attempts = 0
                    self.handle_voice_command(command)
                    return
                else:
                    self.pending_type_attempts += 1
                    if self.pending_type_attempts >= 2:
                        self.pending_type_text = None
                        self.pending_type_fields = []
                        self.pending_type_attempts = 0
                        self.say("I couldn't match that to a field, so I cancelled typing. Say type again with a field name, or focus a field first.")
                    else:
                        self.say(f"I still need a field choice. Say a field name or its number: {choices}. Say cancel to stop.")
                    return

            text = self.pending_type_text
            self.pending_type_text = None
            self.pending_type_fields = []
            self.pending_type_attempts = 0
            self.type_into_active_field(text, selected_field=selected_field)
            return

        if self.pending_chrome_type:
            self.pending_chrome_type = False
            if normalized in ("cancel", "cancel typing"):
                self.say("Typing cancelled.")
                return
            text, destination = self.parse_type_payload(command.strip())
            if text:
                self.type_into_active_field(text, destination=destination)
            else:
                self.say("I didn't catch what to type. Say the words, or say cancel.")
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

        if not self.notes_active:
            command_parts = split_compound_commands(command)
            if len(command_parts) > 1:
                for part_index, command_part in enumerate(command_parts):
                    self.handle_voice_command(command_part)
                    if (part_index + 1 < len(command_parts) and
                            re.match(r"^(?:open|launch|start|new\s+tab)\b", command_part, re.IGNORECASE) and
                            re.match(r"^type\b", command_parts[part_index + 1], re.IGNORECASE)):
                        time.sleep(0.8)
                return

        if "save notes" in normalized or "save note" in normalized:
            self.save_notes()
            return

        if ("open notepad" in normalized or
                normalized in ("open notes", "launch notepad", "launch notes")):
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

        # An unrecognized delete phrase must not fall through to broad
        # natural-language close or exit handling.
        if normalized == "delete" or normalized.startswith("delete "):
            self.say("I couldn't interpret that delete command. Select text first, or say 'delete last three words'.")
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

    @staticmethod
    def parse_type_payload(payload):
        """Split spoken text from an optional destination such as 'in search'."""
        payload = payload.strip()
        destination_first = re.match(
            r"^(?:in|into|to)\s+(?:the\s+)?(.+?)\s+(?:text\s+)?(field|box|bar|input)\s+(.+?)\s*[.!?]*$",
            payload,
            re.IGNORECASE,
        )
        if destination_first:
            destination = destination_first.group(1).strip()
            text = destination_first.group(3).strip()
            destination = re.sub(r"^(?:(?:the|current|active)\s+)+", "", destination, flags=re.IGNORECASE)
            if WindowsAppController.normalize_name(destination) in ("text", ""):
                return text, None
            return text, destination

        destination_match = re.match(
            r"^(.+?)\s+(?:in|into|to)\s+(?:the\s+)?(.+?)\s*[.!?]*$",
            payload,
            re.IGNORECASE,
        )
        if not destination_match:
            return payload, None

        text = destination_match.group(1).strip()
        destination = re.sub(r"[.!?,]+$", "", destination_match.group(2)).strip()
        destination = re.sub(r"\s+please$", "", destination, flags=re.IGNORECASE).strip()
        normalized_destination = WindowsAppController.normalize_name(destination)
        field_suffix = re.search(r"\b(?:text\s+)?(?:field|box|bar|input)\s*$", normalized_destination)
        generic_suffix = re.fullmatch(r"(?:text\s+)?(?:field|box|input)", normalized_destination)
        known_field_terms = {
            "search", "find", "query", "filter", "address", "url", "website",
            "email", "mail", "username", "password", "message", "chat", "comment",
            "subject", "name", "title", "phone", "recipient", "to", "from",
        }
        destination_base = re.sub(
            r"\b(?:text\s+)?(?:field|box|bar|input)\s*$", "", normalized_destination
        )
        destination_base = re.sub(r"^(?:(?:the|current|active)\s+)+", "", destination_base).strip()
        destination_words = destination_base.split()
        has_known_field_term = bool(destination_words and destination_words[-1] in known_field_terms)
        if not (field_suffix or has_known_field_term):
            return payload, None

        if generic_suffix:
            return text, None
        destination = re.sub(
            r"\b(?:text\s+)?(?:field|box|bar|input)\s*$", "", destination,
            flags=re.IGNORECASE,
        )
        destination = re.sub(r"^(?:(?:the|current|active)\s+)+", "", destination, flags=re.IGNORECASE)
        destination = destination.strip(" .,!?")
        return text, destination or None

    def select_pending_type_field(self, spoken_choice):
        """Resolve a spoken number or label against the pending field choices."""
        choice = WindowsAppController.normalize_name(spoken_choice.strip(" .!?"))
        if not choice:
            return None
        ordinal_map = {
            "first": 1, "one": 1, "1": 1, "first field": 1, "option one": 1,
            "second": 2, "two": 2, "2": 2, "second field": 2, "option two": 2,
            "third": 3, "three": 3, "3": 3, "third field": 3, "option three": 3,
            "fourth": 4, "four": 4, "4": 4, "fourth field": 4, "option four": 4,
        }
        choice = re.sub(r"^(?:the|use|choose|select)\s+", "", choice)
        choice = re.sub(r"\b(?:text\s+)?(?:field|box|bar|input)\s*$", "", choice).strip()
        for prefix, number in (("first ", 1), ("second ", 2), ("third ", 3), ("fourth ", 4)):
            if choice.startswith(prefix):
                choice = str(number)
                break
        number = ordinal_map.get(choice)
        if number and number <= len(self.pending_type_fields):
            return self.pending_type_fields[number - 1]

        matching_fields = []
        for field in self.pending_type_fields:
            label = WindowsAppController.normalize_name(field["label"])
            choice_words = set(choice.split())
            label_words = set(label.split())
            if choice == label or choice in label or label in choice or choice_words.issubset(label_words):
                matching_fields.append(field)
        choice_words = set(choice.split())
        if "search" in choice_words and not choice_words & {"address", "url"}:
            page_search_fields = [
                field for field in matching_fields if not field.get("is_address_bar", False)
            ]
            matching_fields = page_search_fields
        if len(matching_fields) > 1:
            labels = {WindowsAppController.normalize_name(field["label"]) for field in matching_fields}
            if len(labels) == 1:
                return max(matching_fields, key=lambda field: (
                    field["focused"], field["control_type"] == "Edit",
                    -field["bounds"][1], -field["bounds"][0],
                ))
        return matching_fields[0] if len(matching_fields) == 1 else None

    def type_into_active_field(self, text, destination=None, selected_field=None):
        """Focus a UIA text field when possible, then paste the recognized dictation."""
        text = text.strip()
        if not text:
            self.say("I didn't catch any words to type.")
            return

        if selected_field is None:
            fields = WindowsAppController.find_editable_fields(self.command_target_hwnd)
            if destination:
                selector = WindowsAppController.normalize_name(destination)
                selector_aliases = {
                    "url": ("url", "address"), "website": ("website", "address", "url"),
                    "address": ("address", "url"), "email": ("email", "mail"),
                    "mail": ("mail", "email"), "search": ("search", "find", "query"),
                    "find": ("find", "search", "query"), "query": ("query", "search"),
                }
                field_hints = {
                    "search", "find", "query", "filter", "address", "url", "website",
                    "email", "mail", "username", "password", "message", "chat", "comment",
                    "subject", "name", "title", "phone", "recipient", "to", "from",
                }
                selector_hints = [word for word in selector.split() if word in field_hints]
                if selector_hints:
                    selector_terms = tuple(
                        alias
                        for hint in selector_hints
                        for alias in selector_aliases.get(hint, (hint,))
                    )
                else:
                    selector_terms = (selector,)
                matching_fields = []
                for field in fields:
                    searchable = WindowsAppController.normalize_name(
                        f"{field['label']} {field.get('automation_id', '')}"
                    )
                    searchable_words = set(searchable.split())
                    if any(
                        term and set(term.split()).issubset(searchable_words)
                        for term in selector_terms
                    ):
                        matching_fields.append(field)
                selector_words = set(selector.split())
                address_requested = bool(selector_words & {"address", "url", "omnibox"})
                search_requested = bool(selector_words & {"search", "find", "query", "filter"})
                if address_requested:
                    address_fields = [
                        field for field in matching_fields if field.get("is_address_bar", False)
                    ]
                    if address_fields:
                        matching_fields = address_fields
                elif search_requested:
                    page_search_fields = [
                        field for field in matching_fields if not field.get("is_address_bar", False)
                    ]
                    matching_fields = page_search_fields
                if not matching_fields:
                    self.say(f"I couldn't find a {destination} field in the active app. Focus it and try again, or name another field.")
                    return
                if len(matching_fields) > 1:
                    self.ask_which_type_field(text, matching_fields)
                    return
                selected_field = matching_fields[0]
            else:
                focused_fields = [
                    field for field in fields
                    if field["focused"] and field["control_type"] in ("Edit", "ComboBox")
                ]
                if len(focused_fields) == 1:
                    selected_field = focused_fields[0]
                elif len(focused_fields) > 1:
                    self.ask_which_type_field(text, focused_fields)
                    return
                else:
                    search_fields = [field for field in fields if field["search_score"]]
                    if len(search_fields) == 1:
                        selected_field = search_fields[0]
                    elif not search_fields and len(fields) == 1:
                        selected_field = fields[0]
                    elif fields:
                        candidates = search_fields if search_fields else fields
                        self.ask_which_type_field(text, candidates)
                        return
                    else:
                        self.say("I couldn't locate an editable field in the active app. Focus the field first, or say ‘type cats in the search field’ so I can look for it.")
                        return

        try:
            if self.chrome_controller is None:
                self.chrome_controller = ChromeController()
            field_name = "the focused field"
            if selected_field is not None:
                selected_field["control"].set_focus()
                field_name = selected_field["label"]
            self.chrome_controller.type_text_foreground(text)
            self.last_typed_text = text
            self.select_all_pending_delete = False
            if selected_field and "password" in WindowsAppController.normalize_name(field_name):
                self.say("I typed the recognized dictation into the password field.")
            else:
                self.say(f'I heard "{text}" and typed it into {field_name}.')
        except Exception as exc:
            self.say(f"I couldn't type into the selected field: {exc}")

    def ask_which_type_field(self, text, fields):
        self.pending_type_text = text
        self.pending_type_fields = fields
        self.pending_type_attempts = 0
        choices = ", ".join(
            f"{field['label']} (say {index})"
            for index, field in enumerate(fields, start=1)
        )
        self.say(f"I heard {text}. Which field should I use? I found {choices}. Say a field name or its number, or say cancel.")

    def handle_chrome_command(self, command, normalized):
        normalized = normalized.strip(" .!?,")
        close_tab_match = re.fullmatch(
            r"(?:please\s+)?close\s+(?:(?:the|current|active)\s+)?tab"
            r"(?:\s+in\s+(?:the\s+)?(?:current|active)\s+application)?"
            r"(?:\s+please)?",
            normalized,
        )
        search_match = re.match(r"^search(?:\s+for)?\s+(.+?)\s*$", command.strip(), re.IGNORECASE)
        search_in_browser_match = re.match(
            r"^search\s+in\s+(chrome|google\s+chrome|edge|microsoft\s+edge|brave|firefox|opera)\s+for\s+(.+?)\s*$",
            command.strip(), re.IGNORECASE,
        )
        type_match = re.match(r"^type(?:\s+(.+))?$", command.strip(), re.IGNORECASE)
        delete_match = re.match(r"^delete\s+last\s+(.+?)\s+words?$", normalized)
        if normalized == "search":
            self.say("What would you like me to search for?")
            return True
        if not (normalized == "open chrome" or search_match or search_in_browser_match or type_match or delete_match or
                normalized in ("delete", "delete word") or
                normalized in ("reload", "reload tab", "go back", "go forward", "exit chrome",
                               "new tab", "close tab", "next tab", "previous tab", "prev tab",
                               "switch to next tab", "switch to previous tab", "switch to prev tab",
                               "scroll down", "scroll up",
                               "enter", "press enter", "hit enter", "return", "press return", "hit return", "submit", "delete", "delete word",
                               "select all", "select everything", "copy", "cut", "paste", "payst", "pay st",
                               "undo", "redo") or close_tab_match):
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
            control_shortcuts = {
                "select all": (controller.VK_CONTROL, ord("A")),
                "select everything": (controller.VK_CONTROL, ord("A")),
                "copy": (controller.VK_CONTROL, ord("C")),
                "cut": (controller.VK_CONTROL, ord("X")),
                "paste": (controller.VK_CONTROL, ord("V")),
                "payst": (controller.VK_CONTROL, ord("V")),
                "pay st": (controller.VK_CONTROL, ord("V")),
                "undo": (controller.VK_CONTROL, ord("Z")),
                "redo": (controller.VK_CONTROL, ord("Y")),
            }
            if normalized in control_shortcuts:
                controller.shortcut_foreground(
                    *control_shortcuts[normalized], fallback_hwnd=self.command_target_hwnd
                )
                if normalized in ("select all", "select everything"):
                    self.select_all_pending_delete = True
                elif normalized in ("cut", "paste", "payst", "pay st", "undo", "redo"):
                    self.select_all_pending_delete = False
                action_messages = {
                    "select all": "Selected all text",
                    "select everything": "Selected all text",
                    "copy": "Copied the selection",
                    "cut": "Cut the selection",
                    "paste": "Pasted the clipboard contents",
                    "payst": "Pasted the clipboard contents",
                    "pay st": "Pasted the clipboard contents",
                    "undo": "Undid the last action",
                    "redo": "Redid the last action",
                }
                self.say(f"{action_messages[normalized]} in the active application.")
            elif type_match:
                text, destination = self.parse_type_payload((type_match.group(1) or "").strip())
                if text:
                    self.type_into_active_field(text, destination=destination)
                else:
                    self.pending_chrome_type = True
                    self.say("What should I type? I'll use the focused field, or find a clear search field. You can say ‘cats in the search field’. Say cancel to stop.")
            elif normalized == "delete":
                if self.select_all_pending_delete:
                    controller.shortcut_foreground(
                        controller.VK_DELETE, fallback_hwnd=self.command_target_hwnd
                    )
                    self.select_all_pending_delete = False
                    self.say("Deleted the selected text in the active application.")
                else:
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
                self.last_opened_browser = "chrome"
                self.say("Opening Chrome.")
            elif search_match or search_in_browser_match:
                explicit_browser = None
                if search_in_browser_match:
                    explicit_browser = WindowsAppController.browser_name(search_in_browser_match.group(1))
                    query = search_in_browser_match.group(2).strip()
                else:
                    query = search_match.group(1).strip()
                    suffix_match = re.match(
                        r"^(.+?)\s+in\s+(chrome|google\s+chrome|edge|microsoft\s+edge|brave|firefox|opera)$",
                        query, re.IGNORECASE,
                    )
                    if suffix_match:
                        query = suffix_match.group(1).strip()
                        explicit_browser = WindowsAppController.browser_name(suffix_match.group(2))
                if query.lower().strip(" .!?,") in ("it", "that", "this") and self.last_typed_text:
                    query = self.last_typed_text
                if not query:
                    self.say("What would you like me to search for?")
                else:
                    search_url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
                    browser = (explicit_browser or
                               WindowsAppController.foreground_browser(self.command_target_hwnd) or
                               self.last_opened_browser or "chrome")
                    WindowsAppController.open_url_in_browser(browser, search_url)
                    self.last_opened_browser = browser
                    browser_label = {"chrome": "Chrome", "edge": "Microsoft Edge", "brave": "Brave",
                                     "firefox": "Firefox", "opera": "Opera"}.get(browser, browser.title())
                    self.say(f"Searching in {browser_label} for {query}.")
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
                controller.shortcut_foreground(
                    controller.VK_CONTROL, new_tab_key, fallback_hwnd=self.command_target_hwnd
                )
                self.say("Opened a new tab in the active application.")
            elif close_tab_match:
                controller.shortcut_foreground(
                    controller.VK_CONTROL, ord("W"), fallback_hwnd=self.command_target_hwnd
                )
                self.say("Closed the current tab in the active application.")
            elif normalized in ("next tab", "switch to next tab"):
                controller.shortcut_foreground(
                    controller.VK_CONTROL, controller.VK_TAB, fallback_hwnd=self.command_target_hwnd
                )
                self.say("Switched to the next tab.")
            elif normalized in ("previous tab", "prev tab", "switch to previous tab", "switch to prev tab"):
                controller.shortcut_foreground(
                    controller.VK_CONTROL, controller.VK_SHIFT, controller.VK_TAB,
                    fallback_hwnd=self.command_target_hwnd,
                )
                self.say("Switched to the previous tab.")
            elif normalized == "scroll down":
                controller.shortcut_foreground(
                    controller.VK_NEXT, fallback_hwnd=self.command_target_hwnd
                )
                self.say("Scrolling down in the active application.")
            elif normalized == "scroll up":
                controller.shortcut_foreground(
                    controller.VK_PRIOR, fallback_hwnd=self.command_target_hwnd
                )
                self.say("Scrolling up in the active application.")
            elif normalized in ("enter", "press enter", "hit enter", "return", "press return", "hit return", "submit"):
                controller.shortcut_foreground(
                    controller.VK_ENTER, fallback_hwnd=self.command_target_hwnd
                )
                self.say("Submitted the focused field in the active application.")
            elif normalized in ("delete", "delete word"):
                controller.shortcut(controller.VK_CONTROL, controller.VK_BACK)
                self.say("Deleted the previous word in the focused Chrome field.")
        except Exception as exc:
            self.say(f"I couldn't complete that Chrome command: {exc}")
        return True

    def handle_windows_app_command(self, command, normalized):
        if re.fullmatch(
            r"minimi[sz]e\s+all(?:\s+the)?(?:\s+(?:(?:open|opened)\s+)?applications?)?"
            r"(?:\s+are\s+(?:open|opened))?",
            normalized.strip(" .!?"),
        ):
            try:
                counts = WindowsAppController.minimize_all_apps()
                if counts["minimized"]:
                    self.say("Minimized all open applications.")
                elif counts["already_minimized"]:
                    self.say("All open applications are already minimized.")
                else:
                    self.say("There are no open applications to minimize.")
            except Exception as exc:
                self.say(f"I couldn't minimize all applications: {exc}")
            return True

        minimize_match = re.match(
            r"^minimi[sz]e\s+(.+?)\s*[.!?]*$", command.strip(), re.IGNORECASE
        )
        if minimize_match:
            target = minimize_match.group(1).strip()
            target_normalized = WindowsAppController.normalize_name(target)
            if target_normalized in ("astra", "assistant", "the assistant", "companion"):
                return False
            try:
                result = WindowsAppController.minimize_app(target)
                if result is None:
                    self.say(f"{target} is not opened.")
                elif result == "already_minimized":
                    self.say(f"{target} is already minimized.")
                else:
                    self.say(f"Minimized {target}.")
            except Exception as exc:
                self.say(f"I couldn't minimize {target}: {exc}")
            return True

        maximize_match = re.match(
            r"^maximi[sz]e\s+(.+?)\s*[.!?]*$", command.strip(), re.IGNORECASE
        )
        if maximize_match:
            target = maximize_match.group(1).strip()
            target_normalized = WindowsAppController.normalize_name(target)
            if target_normalized in ("astra", "assistant", "the assistant", "companion"):
                return False
            try:
                result = WindowsAppController.maximize_app(target)
                if result is None:
                    self.say(f"{target} is not opened.")
                elif result == "already_maximized":
                    self.say(f"{target} is already maximized.")
                else:
                    self.say(f"Maximized {target}.")
            except Exception as exc:
                self.say(f"I couldn't maximize {target}: {exc}")
            return True

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
                browser = WindowsAppController.browser_name(app_name)
                if browser:
                    self.last_opened_browser = browser
                self.say(f"Opening {app_name}.")
            else:
                closed = WindowsAppController.close_app(target)
                if closed:
                    watch = {
                        "target": target,
                        "hwnds": closed["hwnds"],
                        "pids": closed["pids"],
                        "attempts": 0,
                    }
                    self.pending_app_close_watch = watch
                    self.listener.hold_conversation_event.set()
                    QTimer.singleShot(450, self.check_app_close_confirmation)
                    self.say(f"I asked {target} to close. I'll check whether it needs your choice.")
                else:
                    self.say(f"I couldn't find an open window for {target}.")
        except Exception as exc:
            self.say(str(exc))
        return True

    def check_app_close_confirmation(self):
        watch = self.pending_app_close_watch
        if not watch:
            return
        dialog = WindowsAppController.find_close_confirmation(
            watch["pids"], excluded_hwnds=watch["hwnds"]
        )
        if dialog:
            self.pending_app_close_watch = None
            self.pending_app_close_confirmation = {"watch": watch, "dialog": dialog}
            options = [button["label"] for button in dialog["buttons"]]
            detail = (dialog["text"] or dialog["title"] or watch["target"])[:220]
            self.say(
                f"{watch['target']} needs a choice before closing. It says: {detail}. "
                f"The available choices are {', '.join(options)}. Tell me the option to choose."
            )
            return

        watch["attempts"] += 1
        if watch["attempts"] < 12:
            QTimer.singleShot(450, self.check_app_close_confirmation)
        else:
            self.pending_app_close_watch = None
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.IsWindow.argtypes = (wintypes.HWND,)
            user32.IsWindow.restype = wintypes.BOOL
            user32.IsWindowVisible.argtypes = (wintypes.HWND,)
            user32.IsWindowVisible.restype = wintypes.BOOL
            visible_target_hwnd = next((
                hwnd for hwnd in watch["hwnds"]
                if user32.IsWindow(wintypes.HWND(hwnd)) and user32.IsWindowVisible(wintypes.HWND(hwnd))
            ), None)
            if visible_target_hwnd is None:
                self.listener.hold_conversation_event.clear()
                self.say(f"{watch['target']} closed successfully.")
            else:
                self.listener.hold_conversation_event.clear()
                self.say(
                    f"{watch['target']} is still open, but its confirmation controls weren't accessible to Astra. "
                    "Please choose an option on screen; I won't guess."
                )

    def handle_app_close_confirmation_reply(self, command):
        pending = self.pending_app_close_confirmation
        if not pending:
            return False
        selected = WindowsAppController.choose_close_confirmation(pending["dialog"], command)
        if not selected:
            options = [button["label"] for button in pending["dialog"]["buttons"]]
            self.say(f"I didn't match that to a choice. Say {', '.join(options)}, or say the first, second, or third option.")
            return True

        self.pending_app_close_confirmation = None
        selected_normalized = WindowsAppController.normalize_name(selected)
        dialog_context = WindowsAppController.normalize_name(
            pending["dialog"].get("text", "") + " " + pending["dialog"].get("title", "")
        )
        cancel_choice = selected_normalized in (
            "cancel", "keep open", "cancel close", "cancel closing", "stay",
            "dont close", "don t close", "do not close", "dont exit", "don t exit", "do not exit",
        ) or selected_normalized.startswith("keep ")
        if selected_normalized == "no" and "save" not in dialog_context:
            cancel_choice = True
        if cancel_choice:
            self.pending_app_close_watch = None
            self.listener.hold_conversation_event.clear()
            self.say(f"I selected {selected}. The close operation is cancelled.")
        else:
            watch = pending["watch"]
            watch["attempts"] = 0
            self.pending_app_close_watch = watch
            QTimer.singleShot(500, self.check_app_close_confirmation)
            self.say(f"I selected {selected}. I'll continue and check for any next prompt.")
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
        # Update the overlay immediately; speech generation and playback run in a worker.
        self.set_status(text)
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
