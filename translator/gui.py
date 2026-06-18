"""Cursor-following overlay UI for Dwarf Fortress."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import queue
import threading
import time

import win32api
import win32con
import win32gui
from PyQt6.QtCore import QObject, QPoint, QRect, QTimer, Qt
from PyQt6.QtGui import QAction, QColor, QCursor, QFont, QFontMetrics, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QSystemTrayIcon, QVBoxLayout, QWidget

from pipe_reader import PipeReader
from translator import Translator

logger = logging.getLogger(__name__)

DF_WINDOW_TITLE = "Dwarf Fortress"
RESULT_POLL_INTERVAL_MS = 200
WINDOW_SYNC_INTERVAL_MS = 75
CTRL_POLL_INTERVAL_MS = 50
CTRL_TOGGLE_COOLDOWN_SECONDS = 0.40
CURSOR_OFFSET_X = 22
CURSOR_OFFSET_Y = 10
MIN_COLUMNS = 80
MIN_ROWS = 25
TOOLTIP_MARGIN = 12


@dataclass(frozen=True)
class Span:
    y: int
    x_start: int
    x_end: int


@dataclass(frozen=True)
class TextBlock:
    text: str
    spans: tuple[Span, ...] = field(default_factory=tuple)

    def matches(self, tile_x: int, tile_y: int) -> bool:
        return any(span.y == tile_y and span.x_start <= tile_x <= span.x_end for span in self.spans)


_TranslatedFrame = list[tuple[TextBlock, str]]


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


def _group_text_blocks(entries: list[tuple[str, int, int, int]]) -> list[TextBlock]:
    max_intra_row_gap = 1
    max_x_margin_diff = 4
    sidebar_x_diff = 8

    def is_kept(text: str) -> bool:
        return any(char.isalpha() for char in text) or any(char in ".,!?->" for char in text)

    rows: dict[int, list[tuple[int, str]]] = {}
    for text, _justify, x, y in entries:
        if is_kept(text):
            rows.setdefault(y, []).append((x, text))

    row_segments: list[tuple[int, int, int, str]] = []
    for y in sorted(rows.keys()):
        tokens = sorted(rows[y])
        cluster: list[str] = []
        x_start = tokens[0][0]
        prev_x = tokens[0][0]
        prev_len = 0
        x_end = x_start

        for x, text in tokens:
            gap = x - prev_x - prev_len
            if prev_len > 0 and gap > max_intra_row_gap:
                joined = _join_tokens(cluster)
                if any(char.isalpha() for char in joined):
                    row_segments.append((y, x_start, x_end, joined))
                cluster = []
                x_start = x
                x_end = x
            cluster.append(text)
            prev_x = x
            prev_len = len(text)
            x_end = max(x_end, x + max(1, len(text)))

        if cluster:
            joined = _join_tokens(cluster)
            if any(char.isalpha() for char in joined):
                row_segments.append((y, x_start, x_end, joined))

    if not row_segments:
        return []

    blocks: list[TextBlock] = []
    pending_text = row_segments[0][3]
    pending_spans = [Span(row_segments[0][0], row_segments[0][1], row_segments[0][2])]
    pending_y = row_segments[0][0]
    pending_x = row_segments[0][1]

    for y, x_start, x_end, text in row_segments[1:]:
        y_gap = y - pending_y
        x_margin_diff = abs(x_start - pending_x)
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
            pending_spans.append(Span(y, x_start, x_end))
            pending_y = y
        elif y_gap == 0 and x_margin_diff > sidebar_x_diff:
            blocks.append(TextBlock(text=text, spans=(Span(y, x_start, x_end),)))
        else:
            blocks.append(TextBlock(text=pending_text, spans=tuple(pending_spans)))
            pending_text = text
            pending_spans = [Span(y, x_start, x_end)]
            pending_y = y
            pending_x = x_start

    blocks.append(TextBlock(text=pending_text, spans=tuple(pending_spans)))
    return blocks


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


def _client_rect_screen(hwnd: int) -> QRect | None:
    try:
        if not win32gui.IsWindow(hwnd) or win32gui.IsIconic(hwnd):
            return None
        client = win32gui.GetClientRect(hwnd)
        left, top = win32gui.ClientToScreen(hwnd, (client[0], client[1]))
        right, bottom = win32gui.ClientToScreen(hwnd, (client[2], client[3]))
        if right <= left or bottom <= top:
            return None
        return QRect(left, top, right - left, bottom - top)
    except Exception as exc:
        logger.debug("Failed to get DF client rect: %s", exc)
        return None


class CursorOverlay(QWidget):
    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(TOOLTIP_MARGIN, TOOLTIP_MARGIN, TOOLTIP_MARGIN, TOOLTIP_MARGIN)

        self._label = QLabel()
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self._label.setStyleSheet(
            """
            QLabel {
                color: #f3f3f3;
                background-color: rgba(16, 16, 16, 200);
                border: 1px solid rgba(255, 255, 255, 32);
                border-radius: 9px;
                padding: 10px 12px;
                font-family: 'Meiryo UI', 'Yu Gothic UI', sans-serif;
                font-size: 13px;
            }
            """
        )
        layout.addWidget(self._label)

        metrics = QFontMetrics(self._label.font())
        self._content_width = max(420, metrics.averageCharWidth() * 50)
        self._label.setFixedWidth(self._content_width)

    def show_translation(self, text: str, cursor_pos: QPoint, client_rect: QRect) -> None:
        self._label.setText(text)
        self._label.adjustSize()
        self.adjustSize()
        self._apply_click_through()

        x = cursor_pos.x() + CURSOR_OFFSET_X
        y = cursor_pos.y() + CURSOR_OFFSET_Y

        if x + self.width() > client_rect.right():
            x = max(client_rect.left(), cursor_pos.x() - self.width() - CURSOR_OFFSET_X)
        if y + self.height() > client_rect.bottom():
            y = max(client_rect.top(), client_rect.bottom() - self.height())

        self.move(x, y)
        if not self.isVisible():
            self.show()

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
            logger.debug("Failed to set click-through overlay styles: %s", exc)


class OverlayController(QObject):
    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self._app = app
        self._translator = Translator()
        self._reader = PipeReader(on_frame=self.on_frame)
        self._overlay = CursorOverlay()
        self._raw_queue: queue.Queue[list[TextBlock]] = queue.Queue(maxsize=4)
        self._result_queue: queue.Queue[_TranslatedFrame] = queue.Queue(maxsize=4)
        self._overlay_enabled = True
        self._connected = False
        self._ctrl_was_down = False
        self._last_ctrl_toggle = 0.0
        self._last_window_found = False
        self._last_hwnd: int | None = None
        self._current_frame: _TranslatedFrame = []

        self._tray_icon_on = _build_tray_icon(QColor(255, 255, 255))
        self._tray_icon_off = _build_tray_icon(QColor(140, 140, 140))
        self._tray = self._create_tray()

        self._start_translation_worker()
        self._start_timers()
        self._reader.start()
        self._refresh_state()

    def shutdown(self) -> None:
        self._reader.stop()
        self._overlay.hide()
        self._tray.hide()

    def on_frame(self, entries: list[tuple[str, int, int, int]]) -> None:
        blocks = _group_text_blocks(entries)
        if not blocks:
            return

        self._connected = True
        try:
            self._raw_queue.put_nowait(blocks)
        except queue.Full:
            pass

    def toggle_overlay(self, source: str) -> None:
        self._overlay_enabled = not self._overlay_enabled
        logger.info(
            "Overlay toggled %s by %s",
            "on" if self._overlay_enabled else "off",
            source,
        )
        self._refresh_state()

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
            blocks = self._raw_queue.get()
            translated = self._translator.translate_batch([block.text for block in blocks])
            frame = list(zip(blocks, translated))
            try:
                self._result_queue.put_nowait(frame)
            except queue.Full:
                pass

    def _start_timers(self) -> None:
        self._result_timer = QTimer(self)
        self._result_timer.timeout.connect(self._poll_result_queue)
        self._result_timer.start(RESULT_POLL_INTERVAL_MS)

        self._sync_timer = QTimer(self)
        self._sync_timer.timeout.connect(self._sync_overlay)
        self._sync_timer.start(WINDOW_SYNC_INTERVAL_MS)

        self._ctrl_timer = QTimer(self)
        self._ctrl_timer.timeout.connect(self._poll_ctrl_key)
        self._ctrl_timer.start(CTRL_POLL_INTERVAL_MS)

    def _poll_result_queue(self) -> None:
        latest: _TranslatedFrame | None = None
        while True:
            try:
                latest = self._result_queue.get_nowait()
            except queue.Empty:
                break

        if latest is not None:
            self._current_frame = latest
            self._refresh_state()

    def _sync_overlay(self) -> None:
        hwnd = _find_df_window()
        self._last_hwnd = hwnd
        self._last_window_found = hwnd is not None

        if not self._overlay_enabled or hwnd is None:
            self._overlay.hide()
            self._refresh_state()
            return

        client_rect = _client_rect_screen(hwnd)
        if client_rect is None:
            self._overlay.hide()
            self._refresh_state()
            return

        cursor_pos = QCursor.pos()
        if not client_rect.contains(cursor_pos):
            self._overlay.hide()
            self._refresh_state()
            return

        hovered_translation = self._translation_for_cursor(cursor_pos, client_rect)
        if hovered_translation:
            self._overlay.show_translation(hovered_translation, cursor_pos, client_rect)
        else:
            self._overlay.hide()

        self._refresh_state()

    def _translation_for_cursor(self, cursor_pos: QPoint, client_rect: QRect) -> str | None:
        if not self._current_frame:
            return None

        max_col = max(
            span.x_end
            for block, _translation in self._current_frame
            for span in block.spans
        )
        max_row = max(
            span.y
            for block, _translation in self._current_frame
            for span in block.spans
        )

        cols = max(MIN_COLUMNS, max_col + 1)
        rows = max(MIN_ROWS, max_row + 1)
        cell_width = max(1.0, client_rect.width() / cols)
        cell_height = max(1.0, client_rect.height() / rows)

        best: tuple[float, str] | None = None
        for block, translation in self._current_frame:
            left = min(span.x_start for span in block.spans) * cell_width + client_rect.left()
            right = (max(span.x_end for span in block.spans) + 1) * cell_width + client_rect.left()
            top = min(span.y for span in block.spans) * cell_height + client_rect.top()
            bottom = (max(span.y for span in block.spans) + 1) * cell_height + client_rect.top()

            hover_rect = QRect(
                int(left - cell_width * 1.5),
                int(top - cell_height * 0.8),
                int(max(1.0, (right - left) + cell_width * 3.0)),
                int(max(1.0, (bottom - top) + cell_height * 1.6)),
            )

            if hover_rect.contains(cursor_pos):
                return translation

            dx = 0.0
            if cursor_pos.x() < hover_rect.left():
                dx = hover_rect.left() - cursor_pos.x()
            elif cursor_pos.x() > hover_rect.right():
                dx = cursor_pos.x() - hover_rect.right()

            dy = 0.0
            if cursor_pos.y() < hover_rect.top():
                dy = hover_rect.top() - cursor_pos.y()
            elif cursor_pos.y() > hover_rect.bottom():
                dy = cursor_pos.y() - hover_rect.bottom()

            distance = dx + dy * 2.5
            if best is None or distance < best[0]:
                best = (distance, translation)

        if best and best[0] <= cell_width * 10:
            return best[1]
        return None

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

    def _refresh_state(self) -> None:
        state = "ON" if self._overlay_enabled else "OFF"
        window_state = "DF detected" if self._last_window_found else "waiting for Dwarf Fortress"
        connection = "connected" if self._connected else "waiting for text"

        self._toggle_action.setText("Turn overlay off" if self._overlay_enabled else "Turn overlay on")
        self._tray.setIcon(self._tray_icon_on if self._overlay_enabled else self._tray_icon_off)
        self._tray.setToolTip(
            f"DFJP overlay: {state}\n{window_state}\n{connection}\n{self._translator.engine_name}"
        )

        if not self._overlay_enabled:
            self._overlay.hide()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.toggle_overlay("tray click")
