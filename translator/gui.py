"""Overlay-based translation UI for Dwarf Fortress."""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

import win32api
import win32con
import win32gui
from PyQt6.QtCore import QObject, QTimer, Qt
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QHeaderView,
    QLabel,
    QMenu,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pipe_reader import PipeReader
from translator import Translator

logger = logging.getLogger(__name__)

_Frame = tuple[list[str], list[str]]

DF_WINDOW_TITLE = "Dwarf Fortress"
CTRL_POLL_INTERVAL_MS = 50
CTRL_TOGGLE_COOLDOWN_SECONDS = 0.40
RESULT_POLL_INTERVAL_MS = 200
WINDOW_SYNC_INTERVAL_MS = 150


def _join_tokens(tokens: list[str]) -> str:
    if not tokens:
        return ""

    out = tokens[0]
    for token in tokens[1:]:
        if token and token[0] in ".,!?;:":
            out += token
        else:
            out += " " + token
    return out


def _group_by_proximity(entries: list[tuple[str, int, int, int]]) -> list[str]:
    max_intra_row_gap = 1
    max_x_margin_diff = 4
    sidebar_x_diff = 8

    def is_kept(text: str) -> bool:
        return any(char.isalpha() for char in text) or any(char in ".,!?->" for char in text)

    rows: dict[int, list[tuple[int, str]]] = {}
    for text, _justify, x, y in entries:
        if is_kept(text):
            rows.setdefault(y, []).append((x, text))

    segments: list[tuple[int, int, str]] = []
    for y in sorted(rows.keys()):
        tokens = sorted(rows[y])
        cluster: list[str] = []
        x_start = tokens[0][0]
        prev_x, prev_len = tokens[0][0], 0

        for x, text in tokens:
            gap = x - prev_x - prev_len
            if prev_len > 0 and gap > max_intra_row_gap:
                joined = _join_tokens(cluster)
                if any(char.isalpha() for char in joined):
                    segments.append((y, x_start, joined))
                cluster = []
                x_start = x
            cluster.append(text)
            prev_x, prev_len = x, len(text)

        if cluster:
            joined = _join_tokens(cluster)
            if any(char.isalpha() for char in joined):
                segments.append((y, x_start, joined))

    if not segments:
        return []

    result: list[str] = []
    pending_y, pending_x, pending_text = segments[0]

    for y, x, text in segments[1:]:
        y_gap = y - pending_y
        x_margin_diff = abs(x - pending_x)
        ends_sentence = pending_text[-1] in ".!?" if pending_text else True
        ends_with_arrow = pending_text.endswith("->")
        pending_has_internal_punct = any(char in ",." for char in pending_text[:-1])
        first_alpha = next((char for char in text if char.isalpha()), None)
        is_continuation = (
            (first_alpha is not None and first_alpha.islower())
            or ends_with_arrow
            or (not ends_sentence and pending_has_internal_punct)
        )

        if (
            y_gap == 1
            and x_margin_diff <= max_x_margin_diff
            and not ends_sentence
            and is_continuation
        ):
            pending_text = f"{pending_text} {text}"
            pending_y = y
        elif y_gap == 0 and x_margin_diff > sidebar_x_diff:
            result.append(text)
        else:
            result.append(pending_text)
            pending_y, pending_x, pending_text = y, x, text

    result.append(pending_text)
    return result


def _find_df_window() -> int | None:
    matches: list[int] = []

    def callback(hwnd: int, _extra) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if DF_WINDOW_TITLE in title:
            matches.append(hwnd)

    win32gui.EnumWindows(callback, None)
    if not matches:
        return None

    foreground = win32gui.GetForegroundWindow()
    if foreground in matches:
        return foreground
    return matches[0]


def _is_ctrl_down() -> bool:
    return bool(
        win32api.GetAsyncKeyState(win32con.VK_LCONTROL) & 0x8000
        or win32api.GetAsyncKeyState(win32con.VK_RCONTROL) & 0x8000
    )


def _build_tray_icon(text_color: QColor) -> QIcon:
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0))
    painter.drawRoundedRect(0, 0, size - 1, size - 1, 10, 10)

    font = QFont("Segoe UI", 34, QFont.Weight.Bold)
    painter.setFont(font)
    painter.setPen(text_color)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "D")
    painter.end()

    return QIcon(pixmap)


class OverlayWindow(QWidget):
    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self._last_hwnd: int | None = None
        self._connected = False
        self._overlay_enabled = True

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._panel = QWidget(self)
        self._panel.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._panel.setStyleSheet(
            """
            QWidget {
                background-color: rgba(18, 18, 18, 180);
                border: 1px solid rgba(255, 255, 255, 35);
                border-radius: 10px;
            }
            """
        )

        panel_layout = QVBoxLayout(self._panel)
        panel_layout.setContentsMargins(12, 12, 12, 12)
        panel_layout.setSpacing(8)

        self._title = QLabel("DFJP Overlay")
        self._title.setStyleSheet(
            "color: #f0f0f0; font-size: 15px; font-weight: 600; background: transparent; border: none;"
        )
        self._title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        panel_layout.addWidget(self._title)

        self._status = QLabel("Waiting for Dwarf Fortress...")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(
            "color: #c8c8c8; font-size: 12px; background: transparent; border: none;"
        )
        self._status.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        panel_layout.addWidget(self._status)

        self._table = QTableWidget()
        self._table.setColumnCount(2)
        self._table.setHorizontalHeaderLabels(["Original", "Japanese"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._table.setWordWrap(True)
        self._table.setShowGrid(True)
        self._table.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.setColumnWidth(0, 280)

        self._table.setStyleSheet(
            """
            QTableWidget {
                background-color: rgba(0, 0, 0, 80);
                color: #f0f0f0;
                gridline-color: rgba(255, 255, 255, 28);
                border: none;
                font-family: 'Meiryo UI', 'Yu Gothic UI', sans-serif;
                font-size: 13px;
            }
            QTableWidget::item {
                background-color: rgba(35, 35, 35, 95);
                border: none;
                padding: 4px;
            }
            QTableWidget::item:alternate {
                background-color: rgba(44, 44, 44, 95);
            }
            QHeaderView::section {
                background-color: rgba(255, 255, 255, 18);
                color: #f0f0f0;
                padding: 4px 8px;
                border: none;
                border-bottom: 1px solid rgba(255, 255, 255, 25);
            }
            """
        )
        panel_layout.addWidget(self._table, stretch=1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        margin = 16
        panel_width = max(420, min(760, int(self.width() * 0.42)))
        panel_height = max(220, self.height() - margin * 2)
        x = max(margin, self.width() - panel_width - margin)
        self._panel.setGeometry(x, margin, panel_width, panel_height)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_click_through)

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def set_overlay_enabled(self, enabled: bool) -> None:
        self._overlay_enabled = enabled

    def set_rows(self, originals: list[str], translated: list[str]) -> None:
        self._table.setRowCount(0)
        for original, translation in zip(originals, translated):
            row = self._table.rowCount()
            self._table.insertRow(row)
            original_item = QTableWidgetItem(original)
            translation_item = QTableWidgetItem(translation)
            original_item.setFlags(original_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            translation_item.setFlags(translation_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 0, original_item)
            self._table.setItem(row, 1, translation_item)
        self._table.resizeRowsToContents()

    def sync_to_window(self, hwnd: int | None, enabled: bool) -> None:
        self._last_hwnd = hwnd
        self._overlay_enabled = enabled

        if not enabled or hwnd is None:
            self.hide()
            return

        try:
            if not win32gui.IsWindow(hwnd) or win32gui.IsIconic(hwnd):
                self.hide()
                return

            client = win32gui.GetClientRect(hwnd)
            left, top = win32gui.ClientToScreen(hwnd, (client[0], client[1]))
            right, bottom = win32gui.ClientToScreen(hwnd, (client[2], client[3]))
            width = max(0, right - left)
            height = max(0, bottom - top)
            if width <= 0 or height <= 0:
                self.hide()
                return

            if self.geometry().x() != left or self.geometry().y() != top or self.width() != width or self.height() != height:
                self.setGeometry(left, top, width, height)

            if not self.isVisible():
                self.show()
            self._apply_click_through()
        except Exception as exc:
            logger.debug("Failed to sync overlay to DF window: %s", exc)
            self.hide()

    def _apply_click_through(self) -> None:
        try:
            hwnd = int(self.winId())
            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            ex_style |= (
                win32con.WS_EX_LAYERED
                | win32con.WS_EX_TRANSPARENT
                | win32con.WS_EX_TOOLWINDOW
                | win32con.WS_EX_NOACTIVATE
            )
            ex_style &= ~win32con.WS_EX_APPWINDOW
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_TOPMOST,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE
                | win32con.SWP_NOSIZE
                | win32con.SWP_NOACTIVATE
                | win32con.SWP_SHOWWINDOW,
            )
        except Exception as exc:
            logger.debug("Failed to apply click-through styles: %s", exc)


class OverlayController(QObject):
    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self._app = app
        self._translator = Translator()
        self._overlay = OverlayWindow()
        self._reader = PipeReader(on_frame=self.on_frame)
        self._raw_queue: queue.Queue[list[str]] = queue.Queue(maxsize=4)
        self._result_queue: queue.Queue[_Frame] = queue.Queue(maxsize=4)
        self._overlay_enabled = True
        self._connected = False
        self._ctrl_was_down = False
        self._last_ctrl_toggle = 0.0
        self._last_window_found = False
        self._last_hwnd: int | None = None

        self._tray_icon_on = _build_tray_icon(QColor(255, 255, 255))
        self._tray_icon_off = _build_tray_icon(QColor(140, 140, 140))
        self._tray = self._create_tray()

        self._start_translation_worker()
        self._start_timers()
        self._reader.start()
        self._refresh_overlay_state()

    def shutdown(self) -> None:
        self._reader.stop()
        self._tray.hide()
        self._overlay.hide()

    def on_frame(self, entries: list[tuple[str, int, int, int]]) -> None:
        grouped = _group_by_proximity(entries)
        if not grouped:
            return

        self._connected = True
        try:
            self._raw_queue.put_nowait(grouped)
        except queue.Full:
            pass

    def toggle_overlay(self, source: str) -> None:
        self._overlay_enabled = not self._overlay_enabled
        logger.info("Overlay toggled %s by %s", "on" if self._overlay_enabled else "off", source)
        self._refresh_overlay_state()

    def _create_tray(self) -> QSystemTrayIcon:
        tray = QSystemTrayIcon(self._tray_icon_on, self._app)
        tray.setToolTip("DFJP overlay: ON")
        tray.activated.connect(self._on_tray_activated)

        menu = QMenu()
        self._toggle_action = QAction("Turn overlay off", menu)
        self._toggle_action.triggered.connect(lambda: self.toggle_overlay("tray menu"))
        menu.addAction(self._toggle_action)

        quit_action = QAction("Exit", menu)
        quit_action.triggered.connect(self._app.quit)
        menu.addSeparator()
        menu.addAction(quit_action)

        tray.setContextMenu(menu)
        tray.show()
        return tray

    def _start_translation_worker(self) -> None:
        worker = threading.Thread(
            target=self._translation_worker,
            daemon=True,
            name="translator",
        )
        worker.start()

    def _translation_worker(self) -> None:
        while True:
            originals = self._raw_queue.get()
            translated = self._translator.translate_batch(originals)
            try:
                self._result_queue.put_nowait((originals, translated))
            except queue.Full:
                pass

    def _start_timers(self) -> None:
        self._result_timer = QTimer(self)
        self._result_timer.timeout.connect(self._poll_result_queue)
        self._result_timer.start(RESULT_POLL_INTERVAL_MS)

        self._sync_timer = QTimer(self)
        self._sync_timer.timeout.connect(self._sync_overlay_to_df)
        self._sync_timer.start(WINDOW_SYNC_INTERVAL_MS)

        self._ctrl_timer = QTimer(self)
        self._ctrl_timer.timeout.connect(self._poll_ctrl_key)
        self._ctrl_timer.start(CTRL_POLL_INTERVAL_MS)

    def _poll_result_queue(self) -> None:
        latest: _Frame | None = None
        while True:
            try:
                latest = self._result_queue.get_nowait()
            except queue.Empty:
                break

        if latest is not None:
            originals, translated = latest
            self._overlay.set_rows(originals, translated)
            self._refresh_overlay_state()

    def _sync_overlay_to_df(self) -> None:
        hwnd = _find_df_window()
        self._last_hwnd = hwnd
        self._last_window_found = hwnd is not None
        self._overlay.sync_to_window(hwnd, self._overlay_enabled)
        self._refresh_overlay_state()

    def _poll_ctrl_key(self) -> None:
        ctrl_down = _is_ctrl_down()
        now = time.monotonic()
        if (
            ctrl_down
            and not self._ctrl_was_down
            and now - self._last_ctrl_toggle >= CTRL_TOGGLE_COOLDOWN_SECONDS
        ):
            self.toggle_overlay("Ctrl")
            self._last_ctrl_toggle = now
        self._ctrl_was_down = ctrl_down

    def _refresh_overlay_state(self) -> None:
        state = "ON" if self._overlay_enabled else "OFF"
        connection = "connected" if self._connected else "waiting for text"
        window_state = "DF detected" if self._last_window_found else "waiting for Dwarf Fortress"
        status = f"Overlay {state} · {window_state} · {connection} · {self._translator.engine_name}"

        self._overlay.set_overlay_enabled(self._overlay_enabled)
        self._overlay.set_status(status)
        self._toggle_action.setText(
            "Turn overlay off" if self._overlay_enabled else "Turn overlay on"
        )
        self._tray.setIcon(self._tray_icon_on if self._overlay_enabled else self._tray_icon_off)
        self._tray.setToolTip(f"DFJP overlay: {state}\n{window_state}\n{connection}")

        if not self._overlay_enabled:
            self._overlay.hide()
        elif self._last_hwnd is not None:
            self._overlay.sync_to_window(self._last_hwnd, True)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.toggle_overlay("tray click")
