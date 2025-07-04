# /usr/bin/env python3
# -*- coding: utf-8 -*-
# ANLEd - A Nano-Like Editor with Ed-like fallback mode where raw mode does not work
# https://github.com/KaiSD/anled
# License: MIT

from __future__ import annotations

import copy
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap
from enum import Enum, auto
from typing import List, Tuple

try:
    import tty
    import termios
    import fcntl
    IS_UNIX = True
except ImportError:
    import msvcrt
    IS_UNIX = False

_VERSION = "0.9.3"

def term_size() -> Tuple[int, int]:
    size = shutil.get_terminal_size(fallback=(80, 24))
    return size.columns, size.lines

def clear_screen() -> None:
    print("\x1b[2J\x1b[H", end="")

def wrap_line(line: str, width: int) -> List[str]:
    if line.endswith("\n"):
        line = line[:-1]
    return textwrap.wrap(line, width=width, replace_whitespace=False) or [""]

def get_clip_text_ps():
    return subprocess.check_output(
        ["powershell", "-NoProfile", "-Command", "Get-Clipboard"]
    ).decode("utf-8", errors="replace")

def set_clip_text_ps(text: str):
    p = subprocess.Popen(["clip"], stdin=subprocess.PIPE)
    p.communicate(input=text.encode("utf-8"))

def _get_char_width(c):
    if c == '\0' or c < ' ' or (0x7f <= ord(c) <= 0x9f):
        return 0
    if 0x1100 <= ord(c) <= 0x115f or \
       0x2329 <= ord(c) <= 0x232a or \
       0x2e80 <= ord(c) <= 0xa4cf and ord(c) != 0x303f or \
       0xac00 <= ord(c) <= 0xd7a3 or \
       0xf900 <= ord(c) <= 0xfaff or \
       0xfe30 <= ord(c) <= 0xfe6f or \
       0xff00 <= ord(c) <= 0xff60 or \
       0xffe0 <= ord(c) <= 0xffe6:
        return 2
    return 1

def visual_len(s):
    return sum(_get_char_width(c) for c in s)

def visual_slice(s, start_col, end_col=None):
    start_idx, current_col = 0, 0
    for i, char in enumerate(s):
        if current_col >= start_col:
            start_idx = i
            break
        current_col += _get_char_width(char)
    else:
        start_idx = len(s)

    if end_col is None:
        return s[start_idx:]

    end_idx = start_idx
    current_col = visual_len(s[:start_idx])
    for i, char in enumerate(s[start_idx:], start_idx):
        if current_col >= end_col:
            end_idx = i
            break
        current_col += _get_char_width(char)
    else:
        end_idx = len(s)

    return s[start_idx:end_idx]

class FallbackEditor:
    APP_NAME = "ANLEd (Fallback)"
    APP_VERSION = _VERSION


    MENU_TEXT = "Commands: q Quit | w/s Move ↑/↓ | u/j Page ↑/↓ | [num] Go to line | h Help"
    HELP_TEXT = (
        "\n".join([
            "=== Help ===",
            "q         Quit (prompts to save if modified)",
            "o         Save current file",
            "w / s     Move selector up/down one line",
            "u / j     Move selector up/down one page",
            "[number]  Go to a specific line number",
            "e         Replace the selected line",
            "d         Delete the selected line",
            "a         Insert a new blank line at the selector",
            "t         Typewriter mode from the selected line.",
            "z         Undo last change (unlimited stack)",
            "h         Toggle this help panel",
        ])
    )
    MENU_LINES = MENU_TEXT.count("\n") + 1
    FOOTER_LINES = MENU_LINES + 3

    def __init__(self, filename: str | None = None, in_memory: bool = False):
        self.path = pathlib.Path(filename) if filename else None
        self.in_memory = in_memory
        self.lines: List[str] = self._load_file() if self.path and not self.in_memory else ["\n"]
        self.undo: List[List[str]] = []
        self.dirty = False
        self.running = False
        self.help_mode = False

        cols, rows = term_size()
        self.term_width = cols
        self.view_height = max(1, rows - self.FOOTER_LINES)
        self.top = 0
        self.selector = 0

    def _load_file(self) -> List[str]:
        if self.path and self.path.exists() and self.path.stat().st_size > 0:
            return self.path.read_text(encoding="utf-8").splitlines(True)
        return ["\n"]

    def _save_file(self) -> None:
        if self.in_memory:
            print("In-memory mode: Save not applicable.")
            return

        if not self.path:
            try:
                filename = input("Save as: ")
                if not filename:
                    print("Save cancelled.")
                    return
                self.path = pathlib.Path(filename)
            except (KeyboardInterrupt, EOFError):
                print("\nSave cancelled.")
                return

        if self.path.exists():
            try:
                overwrite = input(f'File "{self.path}" already exists. Overwrite? (y/N): ')
                if not overwrite.lower().startswith('y'):
                    print("Save cancelled.")
                    return
            except (KeyboardInterrupt, EOFError):
                print("\nSave cancelled.")
                return

        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        content = "".join(self.lines)
        if not content.endswith('\n'):
            content += '\n'
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(self.path)
        self.dirty = False

    def _render_window(self, max_height: int) -> str:
        if max_height <= 0:
            return ""
            
        rendered: List[str] = []
        rows_used = 0
        i = self.top
        while i < len(self.lines) and rows_used < max_height:
            wrapped = wrap_line(self.lines[i], self.term_width - 7)
            for j, chunk in enumerate(wrapped):
                if rows_used >= max_height:
                    break

                prefix = f"{i+1:>4} │ " if j == 0 else "      │ "
                full_line_content = prefix + chunk

                if i == self.selector:
                    rendered.append(f"\x1b[7m{full_line_content:<{self.term_width}}\x1b[0m")
                else:
                    rendered.append(full_line_content)

                rows_used += 1
            i += 1
        rendered.extend([""] * (max_height - rows_used))
        return "\n".join(rendered)

    def _render_footer(self):
        dirty_indicator = "*" if self.dirty else ""
        filename_display = '[In-Memory]' if self.in_memory else (str(self.path) or '[No Name]')
        left_status = f"{self.APP_NAME} {self.APP_VERSION} - {filename_display}{dirty_indicator}"
        
        right_status = f"Line {self.selector + 1}/{len(self.lines)}"
        
        padding = self.term_width - len(left_status) - len(right_status)
        if padding < 0:
            padding = 0
            left_status = left_status[:self.term_width - len(right_status) - 1] + "…"

        status_bar_content = f"{left_status}{' ' * padding}{right_status}"
        
        print(f"\x1b[7m{status_bar_content:<{self.term_width}}\x1b[0m")
        
        print("-" * self.term_width)
        print(self.MENU_TEXT)

    def _edit_line(self) -> None:
        k = self.selector
        print(f"Old {k+1}: {self.lines[k].rstrip()}")
        new = input("New     → ")
        if not new.endswith("\n"):
            new += "\n"
        self.lines[k] = new

    def _delete_line(self) -> None:
        del self.lines[self.selector]
        if self.selector >= len(self.lines):
            self.selector = max(0, len(self.lines) - 1)

    def _add_line(self) -> None:
        text = input("Line text → ")
        if not text.endswith("\n"):
            text += "\n"
        self.lines.insert(self.selector, text)

    def _typewriter(self) -> None:
        idx = self.selector
        blank_streak = 0
        
        clear_screen()
        print("--- Typewriter Mode ---")
        print("(Press Enter twice on an empty line to exit)")
        print("-" * self.term_width)
        
        context_start = max(0, idx - 5)
        for i in range(context_start, idx):
            print(f"{i+1:>4} │ {self.lines[i].rstrip()}")
        
        while True:
            if idx < len(self.lines):
                print(f"\x1b[36m{idx+1:>4} ≡ {self.lines[idx].rstrip()}\x1b[0m")

            prompt = f"{idx+1:>4} │ "
            new = input(prompt)
            
            print("\x1b[1A\x1b[K", end="")
            print(f"{idx+1:>4} │ {new}")

            if new == "":
                blank_streak += 1
                if blank_streak >= 2:
                    break
            else:
                blank_streak = 0
            
            newline = new + "\n"
            if idx < len(self.lines):
                self.lines[idx] = newline
            else:
                while len(self.lines) <= idx:
                    self.lines.append("\n")
                self.lines[idx] = newline
            idx += 1
        self.selector = idx - 1

    def run(self) -> str | None:
        self.running = True
        help_text_lines = self.HELP_TEXT.splitlines()
        help_panel_height = len(help_text_lines)

        while self.running:
            if not self.lines:
                self.lines.append("\n")
            self.selector = max(0, min(len(self.lines) - 1, self.selector))

            visible_height = self.view_height
            if self.help_mode:
                visible_height = max(1, self.view_height - help_panel_height)

            if self.selector < self.top:
                self.top = self.selector
            else:
                rows_used = 0
                for i in range(self.top, self.selector + 1):
                    rows_used += len(wrap_line(self.lines[i], self.term_width - 7))

                if rows_used > visible_height:
                    new_top = self.selector
                    rows_to_fill = visible_height
                    while new_top >= 0:
                        rows_for_line = len(wrap_line(self.lines[new_top], self.term_width - 7))
                        
                        if rows_for_line > rows_to_fill:
                            if new_top < self.selector:
                                new_top += 1
                            break

                        rows_to_fill -= rows_for_line
                        if new_top == 0:
                            break
                        new_top -= 1
                    self.top = new_top
                    
            clear_screen()

            if self.help_mode:
                file_view_height = self.view_height - help_panel_height
                file_view_str = self._render_window(max_height=file_view_height)
                
                if file_view_str:
                    print(file_view_str)
                print("─" * self.term_width)
                print("\n".join(help_text_lines))
            else:
                print(self._render_window(max_height=self.view_height))
            
            self._render_footer()

            raw_cmd = input("> ")
            action = raw_cmd.strip()
            
            page_jump = max(1, visible_height - 1)

            if action.isdigit():
                target_line = int(action) - 1
                if 0 <= target_line < len(self.lines):
                    self.selector = target_line
                continue

            if not action:
                continue

            if action == "q":
                if self.dirty and not self.in_memory:
                    if input("Save changes? (y/N) ").lower().startswith("y"):
                        self._save_file()
                self.running = False

            elif action == "o":
                self._save_file()
            elif action == "w": self.selector = max(0, self.selector - 1)
            elif action == "s": self.selector = min(len(self.lines) - 1, self.selector + 1)
            elif action == "u": self.selector = max(0, self.selector - page_jump)
            elif action == "j": self.selector = min(len(self.lines) - 1, self.selector + page_jump)

            elif action == "h":
                self.help_mode = not self.help_mode

            elif action in {"e", "d", "a", "t"}:
                self.undo.append(copy.deepcopy(self.lines))
                self.dirty = True

                if action == "e" and self.selector < len(self.lines): self._edit_line()
                elif action == "d" and self.selector < len(self.lines): self._delete_line()
                elif action == "a": self._add_line()
                elif action == "t": self._typewriter()

            elif action == "z" and self.undo:
                self.lines[:] = self.undo.pop()
                self.dirty = True

        clear_screen()
        if self.in_memory:
            return "".join(self.lines)
        return None

class Key(Enum):
    CTRL_A, CTRL_C, CTRL_D, CTRL_E, CTRL_F, CTRL_G, CTRL_H, CTRL_L, CTRL_N, CTRL_P, \
    CTRL_Q, CTRL_S, CTRL_V, CTRL_X, CTRL_Y, ENTER, ESCAPE, BACKSPACE, TAB = range(19)
    UP, DOWN, LEFT, RIGHT, HOME, END, DELETE, PAGE_UP, PAGE_DOWN, INSERT = range(19, 29)
    SHIFT_UP, SHIFT_DOWN, SHIFT_LEFT, SHIFT_RIGHT, SHIFT_HOME, SHIFT_END, \
    SHIFT_PAGE_UP, SHIFT_PAGE_DOWN, SHIFT_INSERT, SHIFT_DELETE = range(29, 39)
    CTRL_LEFT, CTRL_RIGHT, CTRL_HOME, CTRL_END, CTRL_UP, CTRL_DOWN, \
    CTRL_INSERT, CTRL_DELETE = range(39, 47)
    CTRL_SHIFT_LEFT, CTRL_SHIFT_RIGHT, CTRL_SHIFT_HOME, CTRL_SHIFT_END, \
    CTRL_SHIFT_UP, CTRL_SHIFT_DOWN, CTRL_SHIFT_INSERT, CTRL_SHIFT_DELETE = range(47, 55)
    CTRL_0, CTRL_1, CTRL_2, CTRL_3, CTRL_4, CTRL_5, CTRL_6, CTRL_7, CTRL_8, \
    CTRL_9 = range(55, 65)
    F1, F2 = range(65, 67)
    UNKNOWN = auto()
    CHAR = auto()

class KeyDecoder:
    def __init__(self):
        self.key_map = {
            '\x1b[A': Key.UP, '\x1b[B': Key.DOWN, '\x1b[C': Key.RIGHT, '\x1b[D': Key.LEFT,
            '\x1b[H': Key.HOME, '\x1b[F': Key.END, '\x1b[2~': Key.INSERT, '\x1b[3~': Key.DELETE,
            '\x1b[5~': Key.PAGE_UP, '\x1b[6~': Key.PAGE_DOWN,
            '\x1b[11~': Key.F1, '\x1bOP': Key.F1,
            '\x1b[12~': Key.F2, '\x1bOQ': Key.F2,
            '\x1b[1;2A': Key.SHIFT_UP, '\x1b[1;2B': Key.SHIFT_DOWN,
            '\x1b[1;2C': Key.SHIFT_RIGHT, '\x1b[1;2D': Key.SHIFT_LEFT,
            '\x1b[1;2H': Key.SHIFT_HOME, '\x1b[1;2F': Key.SHIFT_END,
            '\x1b[2;2~': Key.SHIFT_INSERT, '\x1b[3;2~': Key.SHIFT_DELETE,
            '\x1b[1;5A': Key.CTRL_UP, '\x1b[1;5B': Key.CTRL_DOWN,
            '\x1b[1;5C': Key.CTRL_RIGHT, '\x1b[1;5D': Key.CTRL_LEFT,
            '\x1b[1;5H': Key.CTRL_HOME, '\x1b[1;5F': Key.CTRL_END,
            '\x1b[2;5~': Key.CTRL_INSERT, '\x1b[3;5~': Key.CTRL_DELETE,
            '\x1b[1;6A': Key.CTRL_SHIFT_UP, '\x1b[1;6B': Key.CTRL_SHIFT_DOWN,
            '\x1b[1;6C': Key.CTRL_SHIFT_RIGHT, '\x1b[1;6D': Key.CTRL_SHIFT_LEFT,
            '\x1b[1;6H': Key.CTRL_SHIFT_HOME, '\x1b[1;6F': Key.CTRL_SHIFT_END,
            '\x1b[2;6~': Key.CTRL_SHIFT_INSERT, '\x1b[3;6~': Key.CTRL_SHIFT_DELETE,
        }
        if not IS_UNIX:
            self._setup_windows_ctypes()
    def _setup_windows_ctypes(self):
        import ctypes
        from ctypes import wintypes
        self.STD_INPUT_HANDLE = -10
        self.KEY_EVENT = 0x0001
        self.CTRL_PRESSED = 0x000C
        self.SHIFT_PRESSED = 0x0010
        self.ALT_PRESSED = 0x0003
        class VK:
            BACK=0x08; TAB=0x09; RETURN=0x0D; ESCAPE=0x1B;
            END=0x23; HOME=0x24; LEFT=0x25; UP=0x26; RIGHT=0x27; DOWN=0x28;
            INSERT=0x2D; DELETE=0x2E; PAGE_UP=0x21; PAGE_DOWN=0x22;
            A=0x41; C=0x43; D=0x44; E=0x45; F=0x46; L=0x4C; H=0x48; N=0x4E; P=0x50;
            Q=0x51; S=0x53; V=0x56; X=0x58; Y=0x59;
            F1=0x70; F2=0x71; G=0x47;
            N0=0x30; N1=0x31; N2=0x32; N3=0x33; N4=0x34; N5=0x35; N6=0x36; N7=0x37; N8=0x38; N9=0x39;
        class KEY_EVENT_RECORD(ctypes.Structure):
            _fields_ = [("bKeyDown", wintypes.BOOL),
                        ("wRepeatCount", wintypes.WORD),
                        ("wVirtualKeyCode", wintypes.WORD),
                        ("wVirtualScanCode", wintypes.WORD),
                        ("uChar", wintypes.WCHAR),
                        ("dwControlKeyState", wintypes.DWORD)]
        class INPUT_RECORD(ctypes.Structure):
            class _U(ctypes.Union):
                _fields_ = [("KeyEvent", KEY_EVENT_RECORD)]
            _fields_ = [("EventType", wintypes.WORD), ("Event", _U)]
        self.INPUT_RECORD = INPUT_RECORD
        self.VK = VK
        self.handle = ctypes.windll.kernel32.GetStdHandle(self.STD_INPUT_HANDLE)
    def get_key(self):
        if IS_UNIX:
            return self._get_key_unix()
        else:
            return self._get_key_windows_ctypes()
    def _get_key_windows_ctypes(self):
        import ctypes
        VK = self.VK
        while True:
            record = self.INPUT_RECORD()
            records_read = ctypes.wintypes.DWORD()
            ctypes.windll.kernel32.ReadConsoleInputW(self.handle, ctypes.byref(record), 1, ctypes.byref(records_read))
            if record.EventType == self.KEY_EVENT and record.Event.KeyEvent.bKeyDown:
                key_event = record.Event.KeyEvent
                vk = key_event.wVirtualKeyCode
                char = key_event.uChar
                ctrl_state = key_event.dwControlKeyState
                is_ctrl = (ctrl_state & self.CTRL_PRESSED) != 0
                is_shift = (ctrl_state & self.SHIFT_PRESSED) != 0
                is_alt = (ctrl_state & self.ALT_PRESSED) != 0

                if is_ctrl and is_shift:
                    key_map = {
                        VK.UP: Key.CTRL_SHIFT_UP, VK.DOWN: Key.CTRL_SHIFT_DOWN,
                        VK.LEFT: Key.CTRL_SHIFT_LEFT, VK.RIGHT: Key.CTRL_SHIFT_RIGHT,
                        VK.HOME: Key.CTRL_SHIFT_HOME, VK.END: Key.CTRL_SHIFT_END,
                        VK.INSERT: Key.CTRL_SHIFT_INSERT, VK.DELETE: Key.CTRL_SHIFT_DELETE,
                    }
                    if vk in key_map: return key_map[vk], None
                elif is_ctrl:
                    key_map = {
                        VK.LEFT: Key.CTRL_LEFT, VK.RIGHT: Key.CTRL_RIGHT,
                        VK.UP: Key.CTRL_UP, VK.DOWN: Key.CTRL_DOWN,
                        VK.HOME: Key.CTRL_HOME, VK.END: Key.CTRL_END,
                        VK.INSERT: Key.CTRL_INSERT, VK.DELETE: Key.CTRL_DELETE,
                        VK.A: Key.CTRL_A, VK.C: Key.CTRL_C, VK.D: Key.CTRL_D,
                        VK.E: Key.CTRL_E, VK.F: Key.CTRL_F, VK.G: Key.CTRL_G, VK.H: Key.CTRL_H,
                        VK.L: Key.CTRL_L, VK.N: Key.CTRL_N, VK.P: Key.CTRL_P,
                        VK.Q: Key.CTRL_Q, VK.S: Key.CTRL_S, VK.V: Key.CTRL_V,
                        VK.X: Key.CTRL_X, VK.Y: Key.CTRL_Y
                    }
                    if vk in key_map: return key_map[vk], None
                    if VK.N0 <= vk <= VK.N9:
                        return Key[f"CTRL_{chr(vk)}"], None
                elif is_shift:
                    key_map = {
                        VK.LEFT: Key.SHIFT_LEFT, VK.RIGHT: Key.SHIFT_RIGHT,
                        VK.UP: Key.SHIFT_UP, VK.DOWN: Key.SHIFT_DOWN,
                        VK.HOME: Key.SHIFT_HOME, VK.END: Key.SHIFT_END,
                        VK.INSERT: Key.SHIFT_INSERT, VK.DELETE: Key.SHIFT_DELETE,
                        VK.PAGE_UP: Key.SHIFT_PAGE_UP, VK.PAGE_DOWN: Key.SHIFT_PAGE_DOWN,
                    }
                    if vk in key_map: return key_map[vk], None
                
                if not is_ctrl and not is_shift and not is_alt:
                    key_map = {
                        VK.UP: Key.UP, VK.DOWN: Key.DOWN, VK.LEFT: Key.LEFT, VK.RIGHT: Key.RIGHT,
                        VK.HOME: Key.HOME, VK.END: Key.END, VK.DELETE: Key.DELETE,
                        VK.INSERT: Key.INSERT, VK.PAGE_UP: Key.PAGE_UP, VK.PAGE_DOWN: Key.PAGE_DOWN,
                        VK.BACK: Key.BACKSPACE, VK.TAB: Key.TAB, VK.RETURN: Key.ENTER,
                        VK.ESCAPE: Key.ESCAPE, VK.F1: Key.F1, VK.F2: Key.F2,
                    }
                    if vk in key_map: return key_map[vk], None

                if char and char.isprintable() and not is_ctrl and not is_alt:
                    return Key.CHAR, char
                
                return Key.UNKNOWN, None

    def _get_key_unix(self):
        char = sys.stdin.read(1)
        if char != '\x1b': return self._map_single_char(char)
        seq = char
        fd = sys.stdin.fileno()
        old_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, old_flags | os.O_NONBLOCK)
        try:
            rest_of_seq = sys.stdin.read(5) 
            if rest_of_seq: seq += rest_of_seq
        except (BlockingIOError, InterruptedError): pass
        finally:
            fcntl.fcntl(fd, fcntl.F_SETFL, old_flags)
        
        if seq == '\x1b': return Key.ESCAPE, None
        return self.key_map.get(seq, Key.UNKNOWN), None
        
    def _map_single_char(self, char):
        key_map = {
            '\r': Key.ENTER, '\n': Key.ENTER,
            '\x7f': Key.BACKSPACE, '\b': Key.BACKSPACE,
            '\t': Key.TAB, '\x1b': Key.ESCAPE,
            '\x01': Key.CTRL_A, '\x03': Key.CTRL_C, '\x04': Key.CTRL_D,
            '\x05': Key.CTRL_E, '\x06': Key.CTRL_F, '\x07': Key.CTRL_G, '\x08': Key.CTRL_H,
            '\x0c': Key.CTRL_L, '\x0e': Key.CTRL_N, '\x10': Key.CTRL_P,
            '\x11': Key.CTRL_Q, '\x13': Key.CTRL_S, '\x16': Key.CTRL_V,
            '\x18': Key.CTRL_X, '\x19': Key.CTRL_Y,
        }
        if char in key_map: return key_map[char], None
        if char.isprintable(): return Key.CHAR, char
        return Key.UNKNOWN, None


class GapBuffer:
    MIN_GAP_SIZE = 16

    def __init__(self, text=''):
        encoded_text = text.encode('utf-8')
        initial_gap = max(self.MIN_GAP_SIZE, len(encoded_text) // 2)
        self.buffer = bytearray(len(encoded_text) + initial_gap)
        self.buffer[:len(encoded_text)] = encoded_text
        self.gap_start = len(encoded_text)
        self.gap_end = len(self.buffer)

    def _get_byte_pos(self, char_pos):
        return len(self.to_string()[:char_pos].encode('utf-8'))

    def _resize_if_needed(self, text_len):
        if (self.gap_end - self.gap_start) < text_len:
            new_gap_size = max(self.MIN_GAP_SIZE, text_len, len(self) // 2)
            new_buffer_size = len(self) + new_gap_size
            new_buffer = bytearray(new_buffer_size)

            new_buffer[:self.gap_start] = self.buffer[:self.gap_start]
            content_after_len = len(self.buffer) - self.gap_end
            new_buffer[new_buffer_size - content_after_len:] = self.buffer[self.gap_end:]

            self.buffer = new_buffer
            self.gap_end = new_buffer_size - content_after_len

    def _move_gap(self, char_pos):
        byte_pos = self._get_byte_pos(char_pos)

        if byte_pos == self.gap_start:
            return

        gap_size = self.gap_end - self.gap_start
        if byte_pos < self.gap_start:
            move_len = self.gap_start - byte_pos
            source_start, source_end = byte_pos, self.gap_start
            dest_start, dest_end = self.gap_end - move_len, self.gap_end
            self.buffer[dest_start:dest_end] = self.buffer[source_start:source_end]
            self.gap_start = byte_pos
            self.gap_end = byte_pos + gap_size
        else:
            move_len = byte_pos - self.gap_start
            source_start, source_end = self.gap_end, self.gap_end + move_len
            dest_start, dest_end = self.gap_start, self.gap_start + move_len
            self.buffer[dest_start:dest_end] = self.buffer[source_start:source_end]
            self.gap_start += move_len
            self.gap_end += move_len

    def insert(self, text, char_pos):
        self._move_gap(char_pos)
        encoded_text = text.encode('utf-8')
        text_len = len(encoded_text)
        self._resize_if_needed(text_len)
        self.buffer[self.gap_start : self.gap_start + text_len] = encoded_text
        self.gap_start += text_len

    def delete(self, char_pos, char_len=1):
        if char_len <= 0: return
        start_byte_pos = self._get_byte_pos(char_pos)
        end_byte_pos = self._get_byte_pos(char_pos + char_len)
        
        self._move_gap(char_pos)
        self.gap_end += (end_byte_pos - start_byte_pos)

    def get_slice(self, start_char=None, end_char=None):
        return self.to_string()[start_char:end_char]

    def to_string(self):
        return (self.buffer[:self.gap_start] + self.buffer[self.gap_end:]).decode('utf-8', 'replace')

    def __len__(self):
        return len(self.to_string())

    def __str__(self):
        return self.to_string()

class Editor:
    APP_NAME = "ANLEd"
    APP_VERSION = _VERSION
    LINE_NUM_WIDTH = 7

    HELP_TEXT = textwrap.dedent("""\
        ─────── ANLEd Help (v{version}) ───────
        F1/Ctrl-H: Toggle this help panel
        Ctrl-S / F2:    Save file
        Ctrl-Q / Esc:    Quit editor
        
        [Selection & Clipboard]
        Shift+Move: Select text
        Ctrl-C / Ctrl-Insert:     Copy selection
        Ctrl-X / Shift-Delete:     Cut selection
        Ctrl-V / Shift-Insert:     Paste
        
        [Cursor Movement]
        Arrows:          Move cursor
        Ctrl+Left/Right: Move by word
        Home/End:        Go to start/end of line
        Page Up/Down:    Move by page
        Ctrl+Home/End:   Go to start/end of document
    """).format(version=_VERSION)

    DEFAULT_KEY_BINDINGS = {
        'quit': (Key.CTRL_Q,Key.ESCAPE),
        'save': (Key.CTRL_S, Key.F2),
        'toggle_help': (Key.F1, Key.CTRL_H),
        'copy': (Key.CTRL_C, Key.CTRL_INSERT),
        'cut': (Key.CTRL_X, Key.SHIFT_DELETE),
        'paste': (Key.CTRL_V, Key.SHIFT_INSERT),

        'move_up': (Key.UP, Key.SHIFT_UP, Key.CTRL_UP, Key.CTRL_SHIFT_UP),
        'move_down': (Key.DOWN, Key.SHIFT_DOWN, Key.CTRL_DOWN, Key.CTRL_SHIFT_DOWN),
        'move_left': (Key.LEFT, Key.SHIFT_LEFT),
        'move_right': (Key.RIGHT, Key.SHIFT_RIGHT),
        'move_home': (Key.HOME, Key.SHIFT_HOME),
        'move_end': (Key.END, Key.SHIFT_END),
        'move_page_up': (Key.PAGE_UP, Key.SHIFT_PAGE_UP),
        'move_page_down': (Key.PAGE_DOWN, Key.SHIFT_PAGE_DOWN),
        'move_prev_word': (Key.CTRL_LEFT, Key.CTRL_SHIFT_LEFT),
        'move_next_word': (Key.CTRL_RIGHT, Key.CTRL_SHIFT_RIGHT),
        'move_doc_start': (Key.CTRL_HOME, Key.CTRL_SHIFT_HOME),
        'move_doc_end': (Key.CTRL_END, Key.CTRL_SHIFT_END),

        'delete_back': (Key.BACKSPACE,),
        'delete_forward': (Key.DELETE,),
        'insert_newline': (Key.ENTER,),
        'insert_char': (Key.CHAR,),
    }

    def __init__(self, filename=None, key_bindings=None, in_memory=False):
        self.filename = filename
        self.in_memory = in_memory
        self.buffer = [] 
        
        if self.filename and os.path.exists(self.filename) and not self.in_memory:
            with open(self.filename, encoding="utf-8") as f:
                self.buffer = [GapBuffer(line.rstrip('\n')) for line in f]
        
        if not self.buffer:
            self.buffer.append(GapBuffer(''))

        self.cursor_x = 0
        self.cursor_y = 0
        self.top_line = 0
        self.col_offset = 0
        self.running = True
        self.is_dirty = False
        self.status_message = "HELP: Ctrl-S save | Ctrl-Q quit | F1/Ctrl-H help"
        self.help_mode = False

        self.selection_start_x = -1
        self.selection_start_y = -1
        self.is_selecting = False
        self.clipboard = ""
        self.key_decoder = KeyDecoder()

        self.key_bindings = self.DEFAULT_KEY_BINDINGS.copy()
        if key_bindings:
            self.key_bindings.update(key_bindings)

        self.action_map = {k: action for action, keys in self.key_bindings.items() for k in keys}

        movement_actions = {
            'move_up', 'move_down', 'move_left', 'move_right', 'move_home', 'move_end',
            'move_page_up', 'move_page_down', 'move_prev_word', 'move_next_word',
            'move_doc_start', 'move_doc_end'
        }
        self._plain_movement_keys = {
            k for action in movement_actions
            for k in self.key_bindings.get(action, [])
            if 'SHIFT' not in k.name
        }
        self._shift_movement_keys = {
            k for action in movement_actions
            for k in self.key_bindings.get(action, [])
            if 'SHIFT' in k.name
        }

    def get_terminal_size(self):
        try:
            sz = os.get_terminal_size()
            return sz.columns, sz.lines
        except OSError:
            return 80, 24

    def cursor_char_pos_to_visual(self, y, x):
        return visual_len(str(self.buffer[y])[:x])

    def cursor_visual_pos_to_char(self, y, visual_x):
        line = str(self.buffer[y])
        current_col = 0
        for i, char in enumerate(line):
            if current_col >= visual_x:
                return i
            current_col += _get_char_width(char)
        return len(line)

    def clamp_cursor(self):
        self.cursor_y = max(0, min(self.cursor_y, len(self.buffer) - 1))
        self.cursor_x = max(0, min(self.cursor_x, len(str(self.buffer[self.cursor_y]))))

    def prompt(self, prompt_msg):
        user_input = ""
        while True:
            self.status_message = f"{prompt_msg}{user_input}"
            self.render()
            key, char = self.key_decoder.get_key()
            if key == Key.ENTER:
                self.status_message = ""
                return user_input if user_input else None
            elif key == Key.BACKSPACE:
                user_input = user_input[:-1]
            elif key == Key.ESCAPE:
                self.status_message = ""
                return None
            elif key == Key.CHAR:
                user_input += char

    def get_selection(self):
        if not self.is_selecting or self.selection_start_y == -1:
            return None
        start_y, end_y = sorted([self.selection_start_y, self.cursor_y])
        if self.selection_start_y == self.cursor_y:
            start_x, end_x = sorted([self.selection_start_x, self.cursor_x])
        elif self.selection_start_y < self.cursor_y:
            start_x, end_x = self.selection_start_x, self.cursor_x
        else:
            start_x, end_x = self.cursor_x, self.selection_start_x
        return start_y, start_x, end_y, end_x
        
    def render(self):
        sys.stdout.write('\x1b[?25l')

        width, height = self.get_terminal_size()
        
        help_panel_height = 0
        help_text_lines = []
        if self.help_mode:
            help_text_lines = self.HELP_TEXT.strip().split('\n')
            help_panel_height = len(help_text_lines) + 1

        view_height = height - 2 - help_panel_height
        if view_height < 1: view_height = 1
        
        text_area_width = width - self.LINE_NUM_WIDTH

        if self.cursor_y < self.top_line:
            self.top_line = self.cursor_y
        if self.cursor_y >= self.top_line + view_height:
            self.top_line = self.cursor_y - view_height + 1

        visual_cursor_x = self.cursor_char_pos_to_visual(self.cursor_y, self.cursor_x)
        if visual_cursor_x < self.col_offset:
            self.col_offset = visual_cursor_x
        if visual_cursor_x >= self.col_offset + text_area_width:
            self.col_offset = visual_cursor_x - text_area_width + 1

        output_buffer = []
        selection = self.get_selection()

        for i in range(view_height):
            output_buffer.append(f'\x1b[{i + 1};1H')
            
            buf_idx = self.top_line + i
            if buf_idx < len(self.buffer):
                line_gb = self.buffer[buf_idx]
                line_str_raw = str(line_gb)
                
                line_num_prefix = f"{buf_idx + 1:>{self.LINE_NUM_WIDTH - 3}} | "
                
                if selection:
                    start_y, start_x, end_y, end_x = selection
                    if start_y <= buf_idx <= end_y:
                        sel_start_char = start_x if buf_idx == start_y else 0
                        sel_end_char = end_x if buf_idx == end_y else len(line_str_raw)
                        part1 = line_str_raw[:sel_start_char]
                        part2 = line_str_raw[sel_start_char:sel_end_char]
                        part3 = line_str_raw[sel_end_char:]
                        line_str_formatted = f"{part1}\x1b[7m{part2}\x1b[m{part3}"
                    else:
                        line_str_formatted = line_str_raw
                else:
                    line_str_formatted = line_str_raw

                line_to_render = visual_slice(line_str_formatted, self.col_offset, self.col_offset + text_area_width)
                full_line = f"{line_num_prefix}{line_to_render}"
                output_buffer.append(full_line)
            else:
                tilde_prefix = " " * (self.LINE_NUM_WIDTH - 2) + "~ "
                output_buffer.append(tilde_prefix)
            
            output_buffer.append('\x1b[K')

        for i in range(view_height, height - 2):
            output_buffer.append(f'\x1b[{i + 1};1H\x1b[K')

        if self.help_mode:
            separator_y = view_height + 1
            output_buffer.append(f'\x1b[{separator_y};1H')
            output_buffer.append("─" * width)

            for i, line in enumerate(help_text_lines):
                help_line_y = separator_y + 1 + i
                if help_line_y < height - 1:
                    output_buffer.append(f'\x1b[{help_line_y};1H')
                    output_buffer.append(line.ljust(width))
        
        dirty_indicator = "*" if self.is_dirty else ""
        filename_display = '[In-Memory]' if self.in_memory else (self.filename or '[No Name]')
        left_status = f"{self.APP_NAME} {self.APP_VERSION} - {filename_display}{dirty_indicator}"
        right_status = f"Ln {self.cursor_y + 1}, Col {visual_cursor_x + 1}"
        
        status_bar_content = f"{left_status.ljust(width - len(right_status))}{right_status}"
        output_buffer.append(f"\x1b[{height-1};1H\x1b[7m{status_bar_content}\x1b[m")

        help_bar_content = self.status_message[:width].ljust(width)
        output_buffer.append(f"\x1b[{height};1H{help_bar_content}")
        
        draw_y = self.cursor_y - self.top_line + 1
        draw_x = visual_cursor_x - self.col_offset + 1 + self.LINE_NUM_WIDTH
        output_buffer.append(f'\x1b[{draw_y};{draw_x}H')

        output_buffer.append('\x1b[?25h')
        
        sys.stdout.write("".join(output_buffer))
        sys.stdout.flush()

    def _find_next_word(self):
        x, y = self.cursor_x, self.cursor_y
        line = str(self.buffer[y])
        
        while x < len(line) and not line[x].isspace():
            x += 1
        while x < len(line) and line[x].isspace():
            x += 1
            
        if x >= len(line):
            if y < len(self.buffer) - 1:
                self.cursor_y += 1
                self.cursor_x = 0
            else:
                self.cursor_x = len(line)
        else:
            self.cursor_x = x

    def _find_prev_word(self):
        x, y = self.cursor_x, self.cursor_y
        
        if x == 0 and y > 0:
            self.cursor_y -= 1
            self.cursor_x = len(str(self.buffer[self.cursor_y]))
            return

        x -= 1
        line = str(self.buffer[y])
        while x > 0 and line[x-1].isspace():
            x -= 1
        while x > 0 and not line[x-1].isspace():
            x -= 1
            
        self.cursor_x = x

    def copy_selection(self):
        selection = self.get_selection()
        if not selection: return

        start_y, start_x, end_y, end_x = selection
        if start_y == end_y:
            self.clipboard = self.buffer[start_y].get_slice(start_x, end_x)
        else:
            lines = []
            lines.append(self.buffer[start_y].get_slice(start_x, None))
            for i in range(start_y + 1, end_y):
                lines.append(str(self.buffer[i]))
            lines.append(self.buffer[end_y].get_slice(0, end_x))
            self.clipboard = "\n".join(lines)
        
        if not IS_UNIX:
            set_clip_text_ps(self.clipboard)

        self.status_message = "Copied to clipboard."

    def delete_selection(self):
        selection = self.get_selection()
        if not selection: return

        start_y, start_x, end_y, end_x = selection
        if start_y == end_y:
            self.buffer[start_y].delete(start_x, end_x - start_x)
            self.cursor_x = start_x
        else:
            first_line_slice = self.buffer[start_y].get_slice(0, start_x)
            last_line_slice = self.buffer[end_y].get_slice(end_x, None)
            
            self.buffer[start_y] = GapBuffer(first_line_slice + last_line_slice)
            
            del self.buffer[start_y + 1 : end_y + 1]
            self.cursor_x = start_x
        
        self.cursor_y = start_y
        self.is_dirty = True
        self.is_selecting = False

    def handle_keypress(self, key, char):
        self.status_message = "HELP: Ctrl-S save | Ctrl-Q quit | F1/Ctrl-H help"

        if key in self._plain_movement_keys and self.is_selecting:
            self.is_selecting = False
        
        if key in self._shift_movement_keys and not self.is_selecting:
            self.is_selecting = True
            self.selection_start_x, self.selection_start_y = self.cursor_x, self.cursor_y

        action = self.action_map.get(key)
        
        editing_actions = {'delete_back', 'delete_forward', 'insert_newline', 'insert_char'}
        if self.is_selecting and action in editing_actions:
            self.delete_selection()

        if action == 'quit':
            if self.is_dirty and not self.in_memory:
                response = self.prompt("Save changes before quitting? (y/n): ")
                if response and response.lower() == 'y':
                    if self.save_file(): self.running = False
                elif response and response.lower() == 'n': self.running = False
            else:
                self.running = False
            return
        elif action == 'toggle_help':
            self.help_mode = not self.help_mode
        elif action == 'save': self.save_file()
        elif action == 'copy': self.copy_selection()
        elif action == 'cut':
            self.copy_selection()
            self.delete_selection()
        elif action == 'paste':
            if not IS_UNIX:
                self.clipboard = get_clip_text_ps()
            if self.is_selecting: self.delete_selection()
            lines = self.clipboard.split('\n')
            if len(lines) == 1:
                self.buffer[self.cursor_y].insert(lines[0], self.cursor_x)
                self.cursor_x += len(lines[0])
            else:
                current_line = self.buffer[self.cursor_y]
                line_remainder = current_line.get_slice(self.cursor_x, None)
                current_line.delete(self.cursor_x, len(str(current_line)) - self.cursor_x)
                current_line.insert(lines[0], self.cursor_x)
                
                new_lines = [GapBuffer(line) for line in lines[1:-1]]
                last_line = GapBuffer(lines[-1] + line_remainder)
                
                for i, line in enumerate(new_lines + [last_line], 1):
                    self.buffer.insert(self.cursor_y + i, line)

                self.cursor_y += len(lines) - 1
                self.cursor_x = len(lines[-1])
            self.is_dirty = True
        
        elif action == 'move_up': self.cursor_y -= 1
        elif action == 'move_down': self.cursor_y += 1
        elif action == 'move_left':
            if self.cursor_x > 0: self.cursor_x -= 1
            elif self.cursor_y > 0:
                self.cursor_y -= 1; self.cursor_x = len(str(self.buffer[self.cursor_y]))
        elif action == 'move_right':
            if self.cursor_x < len(str(self.buffer[self.cursor_y])): self.cursor_x += 1
            elif self.cursor_y < len(self.buffer) - 1:
                self.cursor_y += 1; self.cursor_x = 0
        elif action == 'move_home': self.cursor_x = 0
        elif action == 'move_end': self.cursor_x = len(str(self.buffer[self.cursor_y]))
        elif action == 'move_page_up':
            _, height = self.get_terminal_size(); self.cursor_y -= (height - 2)
        elif action == 'move_page_down':
            _, height = self.get_terminal_size(); self.cursor_y += (height - 2)
        elif action == 'move_prev_word': self._find_prev_word()
        elif action == 'move_next_word': self._find_next_word()
        elif action == 'move_doc_start': self.cursor_y, self.cursor_x = 0, 0
        elif action == 'move_doc_end':
            self.cursor_y = len(self.buffer) - 1
            self.cursor_x = len(str(self.buffer[self.cursor_y]))

        elif action == 'delete_back':
            self.is_dirty = True
            if self.cursor_x > 0:
                self.buffer[self.cursor_y].delete(self.cursor_x - 1, 1)
                self.cursor_x -= 1
            elif self.cursor_y > 0:
                line_content = str(self.buffer.pop(self.cursor_y))
                self.cursor_y -= 1
                self.cursor_x = len(str(self.buffer[self.cursor_y]))
                self.buffer[self.cursor_y].insert(line_content, self.cursor_x)
        elif action == 'delete_forward':
            self.is_dirty = True
            if self.cursor_x < len(str(self.buffer[self.cursor_y])):
                self.buffer[self.cursor_y].delete(self.cursor_x, 1)
            elif self.cursor_y < len(self.buffer) - 1:
                line_content = str(self.buffer.pop(self.cursor_y + 1))
                self.buffer[self.cursor_y].insert(line_content, self.cursor_x)
        elif action == 'insert_newline':
            self.is_dirty = True
            current_line = self.buffer[self.cursor_y]
            line_remainder = current_line.get_slice(self.cursor_x, None)
            current_line.delete(self.cursor_x, len(str(current_line)) - self.cursor_x)
            self.buffer.insert(self.cursor_y + 1, GapBuffer(line_remainder))
            self.cursor_y += 1
            self.cursor_x = 0
        elif action == 'insert_char':
            self.is_dirty = True
            self.buffer[self.cursor_y].insert(char, self.cursor_x)
            self.cursor_x += len(char)
        
        self.clamp_cursor()

    def save_file(self):
        if self.in_memory:
            self.status_message = "In-memory mode: Save not applicable."
            self.is_dirty = False 
            return True

        if not self.filename:
            new_filename = self.prompt("Save as: ")
            if new_filename is None:
                self.status_message = "Save cancelled."
                return False
            self.filename = new_filename
        
        if os.path.exists(self.filename):
            overwrite = self.prompt(f'File "{self.filename}" already exists. Overwrite? (y/N): ')
            if overwrite is None or not overwrite.lower().startswith('y'):
                self.status_message = "Save cancelled."
                return False
        
        try:
            with open(self.filename, 'w', encoding="utf-8") as f:
                f.write('\n'.join(str(gb) for gb in self.buffer))
            self.is_dirty = False
            self.status_message = f'Saved {len(self.buffer)} lines to "{self.filename}".'
            return True
        except OSError as e:
            self.status_message = f'Error saving: {e}'
            return False

    def run(self):
        if IS_UNIX:
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
        try:
            if IS_UNIX:
                tty.setraw(sys.stdin.fileno())
            self.render()
            while self.running:
                try:
                    key, char = self.key_decoder.get_key()
                    self.handle_keypress(key, char)
                    if self.running:
                        self.render()
                except KeyboardInterrupt:
                    pass
        finally:
            if IS_UNIX:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            sys.stdout.write('\x1b[2J\x1b[H')
            sys.stdout.flush()
        
        if self.in_memory:
            return '\n'.join(str(gb) for gb in self.buffer)

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="ANLEd - A Nano-Like Editor")
    parser.add_argument("file", nargs="?", help="File to edit")
    parser.add_argument("--nonraw", action="store_true", 
                        help="Use fallback editor instead of raw terminal mode")
    
    args = parser.parse_args()
    file_arg = args.file
    fallback = args.nonraw

    if IS_UNIX:
        try:
            termios.tcgetattr(sys.stdin.fileno())
        except termios.error:
            fallback = True
    
    try:
        if fallback:
            FallbackEditor(file_arg).run()
        else:
            Editor(file_arg).run()
    
    except Exception:
        sys.stdout.write('\x1b[2J\x1b[H')
        sys.stdout.flush()
        import traceback
        print("ANLEd crashed. Please report this issue.")
        traceback.print_exc()
