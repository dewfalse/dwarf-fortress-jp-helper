"""Named Pipe client for receiving text frames from the hook DLL."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from datetime import datetime
from typing import Callable

import pywintypes
import win32file

from config import load_config
from runtime_paths import debug_log_path

PIPE_NAME = r"\\.\pipe\df_translation"
RECONNECT_INTERVAL = 2.0
TEXT_KIND_NORMAL = 0
TEXT_KIND_RICH_BLOCK = 1
TEXT_KIND_RICH_TOKEN = 2


@dataclass(frozen=True)
class TextEntry:
    text: str
    justify: int
    x: int
    y: int
    kind: int = TEXT_KIND_NORMAL
    group_id: int | None = None
    mouse_x: int | None = None
    mouse_y: int | None = None
    mouse_pixel_x: int | None = None
    mouse_pixel_y: int | None = None
    tile_w: int | None = None
    tile_h: int | None = None


class PipeReader:
    """Read hook output and emit one frame of TextEntry values at a time."""

    def __init__(self, on_frame: Callable[[list[TextEntry]], None]) -> None:
        self.on_frame = on_frame
        self._running = False
        self._thread: threading.Thread | None = None
        self._debug_log = load_config().debug_log
        self._log_file = (
            open(debug_log_path(), "a", encoding="utf-8") if self._debug_log else None
        )

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="pipe-reader")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._log_file:
            self._log_file.close()
            self._log_file = None

    def _run(self) -> None:
        while self._running:
            try:
                pipe = win32file.CreateFile(
                    PIPE_NAME,
                    win32file.GENERIC_READ,
                    0,
                    None,
                    win32file.OPEN_EXISTING,
                    0,
                    None,
                )
                try:
                    self._read_loop(pipe)
                finally:
                    win32file.CloseHandle(pipe)
            except pywintypes.error:
                pass
            time.sleep(RECONNECT_INTERVAL)

    def _read_loop(self, pipe) -> None:
        buf = b""
        entries: list[TextEntry] = []
        while self._running:
            try:
                _, data = win32file.ReadFile(pipe, 65536)
                buf += data
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    line = raw_line.decode("utf-8", errors="replace")
                    self._handle_line(line, entries)
            except pywintypes.error:
                break

    def _handle_line(self, line: str, entries: list[TextEntry]) -> None:
        if line == "F":
            if entries:
                self._write_log(entries)
                self.on_frame(list(entries))
            entries.clear()
            return

        if not line.startswith("T\t"):
            return

        parts = line[2:].split("\t")
        if len(parts) >= 12:
            try:
                kind = int(parts[0])
                raw_group_id = int(parts[1])
                justify = int(parts[2])
                x = int(parts[3])
                y = int(parts[4])
                mouse_x = int(parts[5])
                mouse_y = int(parts[6])
                mouse_pixel_x = int(parts[7])
                mouse_pixel_y = int(parts[8])
                tile_w = int(parts[9])
                tile_h = int(parts[10])
            except ValueError:
                kind = TEXT_KIND_NORMAL
                raw_group_id = 0
                justify, x, y = 0, 0, 0
                mouse_x = mouse_y = mouse_pixel_x = mouse_pixel_y = tile_w = tile_h = None
            text = "\t".join(parts[11:])
        elif len(parts) == 4:
            kind = TEXT_KIND_NORMAL
            raw_group_id = 0
            try:
                justify = int(parts[0])
                x = int(parts[1])
                y = int(parts[2])
            except ValueError:
                justify, x, y = 0, 0, 0
            mouse_x = mouse_y = mouse_pixel_x = mouse_pixel_y = tile_w = tile_h = None
            text = parts[3]
        else:
            kind = TEXT_KIND_NORMAL
            raw_group_id = 0
            justify, x, y = 0, 0, 0
            mouse_x = mouse_y = mouse_pixel_x = mouse_pixel_y = tile_w = tile_h = None
            text = parts[-1] if parts else ""

        group_id = raw_group_id if raw_group_id > 0 else None
        mouse_x = mouse_x if mouse_x is not None and mouse_x >= 0 else None
        mouse_y = mouse_y if mouse_y is not None and mouse_y >= 0 else None
        mouse_pixel_x = mouse_pixel_x if mouse_pixel_x is not None and mouse_pixel_x >= 0 else None
        mouse_pixel_y = mouse_pixel_y if mouse_pixel_y is not None and mouse_pixel_y >= 0 else None
        tile_w = tile_w if tile_w is not None and tile_w > 0 else None
        tile_h = tile_h if tile_h is not None and tile_h > 0 else None
        text = text.replace("\\n", "\n").replace("\\t", "\t")
        if text:
            entries.append(
                TextEntry(
                    text=text,
                    kind=kind,
                    group_id=group_id,
                    justify=justify,
                    x=x,
                    y=y,
                    mouse_x=mouse_x,
                    mouse_y=mouse_y,
                    mouse_pixel_x=mouse_pixel_x,
                    mouse_pixel_y=mouse_pixel_y,
                    tile_w=tile_w,
                    tile_h=tile_h,
                )
            )

    def _write_log(self, entries: list[TextEntry]) -> None:
        if not self._log_file:
            return

        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._log_file.write(f"\n=== FRAME {ts} ({len(entries)} texts) ===\n")
        for index, entry in enumerate(entries):
            pixel = ""
            if entry.mouse_x is not None and entry.mouse_y is not None:
                pixel += f" mx={entry.mouse_x:3d} my={entry.mouse_y:3d}"
            if entry.mouse_pixel_x is not None and entry.mouse_pixel_y is not None:
                pixel += f" px={entry.mouse_pixel_x:4d} py={entry.mouse_pixel_y:4d}"
            if entry.tile_w is not None and entry.tile_h is not None:
                pixel += f" tw={entry.tile_w:3d} th={entry.tile_h:3d}"
            self._log_file.write(
                f"[{index:3d}] k={entry.kind} g={entry.group_id or 0} j={entry.justify} "
                f"x={entry.x:3d} y={entry.y:3d}{pixel} {entry.text!r}\n"
            )
        self._log_file.flush()
