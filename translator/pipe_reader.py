"""Named Pipe クライアント。DLL から届くテキストをフレーム単位で受信する。"""

from __future__ import annotations

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


class PipeReader:
    """
    バックグラウンドスレッドで Named Pipe を読み続ける。

    on_frame には `list[tuple[text, justify, x, y]]` を渡す。
    """

    def __init__(self, on_frame: Callable[[list[tuple[str, int, int, int]]], None]) -> None:
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
        entries: list[tuple[str, int, int, int]] = []
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

    def _handle_line(
        self,
        line: str,
        entries: list[tuple[str, int, int, int]],
    ) -> None:
        if line == "F":
            if entries:
                self._write_log(entries)
                self.on_frame(list(entries))
            entries.clear()
            return

        if not line.startswith("T\t"):
            return

        parts = line[2:].split("\t", 3)
        if len(parts) == 4:
            try:
                justify = int(parts[0])
                x = int(parts[1])
                y = int(parts[2])
            except ValueError:
                justify, x, y = 0, 0, 0
            text = parts[3]
        else:
            justify, x, y = 0, 0, 0
            text = parts[-1] if parts else ""

        text = text.replace("\\n", "\n").replace("\\t", "\t")
        if text:
            entries.append((text, justify, x, y))

    def _write_log(self, entries: list[tuple[str, int, int, int]]) -> None:
        if not self._log_file:
            return

        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._log_file.write(f"\n=== FRAME {ts} ({len(entries)} texts) ===\n")
        for index, (text, justify, x, y) in enumerate(entries):
            self._log_file.write(
                f"[{index:3d}] j={justify} x={x:3d} y={y:3d} {text!r}\n"
            )
        self._log_file.flush()
