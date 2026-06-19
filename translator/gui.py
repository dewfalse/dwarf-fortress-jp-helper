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
from PyQt6.QtGui import QAction, QColor, QFont, QFontMetrics, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QSystemTrayIcon, QVBoxLayout, QWidget

from pipe_reader import (
    PipeReader,
    TEXT_KIND_RICH_BLOCK,
    TEXT_KIND_RICH_TOKEN,
    TextEntry,
)
from translator import Translator

logger = logging.getLogger(__name__)

DF_WINDOW_TITLE = "Dwarf Fortress"
RESULT_POLL_INTERVAL_MS = 50
WINDOW_SYNC_INTERVAL_MS = 75
CTRL_POLL_INTERVAL_MS = 50
CTRL_TOGGLE_COOLDOWN_SECONDS = 0.40
CURSOR_OFFSET_X = 22
CURSOR_OFFSET_Y = 10
MIN_COLUMNS = 80
MIN_ROWS = 25
TOOLTIP_MARGIN = 12
MIN_TOOLTIP_WIDTH = 180


@dataclass(frozen=True)
class Span:
    y: int
    x_start: int
    x_end: int
    pixel_left: int | None = None
    pixel_right: int | None = None
    pixel_top: int | None = None
    pixel_bottom: int | None = None


@dataclass(frozen=True)
class TextBlock:
    text: str
    spans: tuple[Span, ...] = field(default_factory=tuple)
    fallback_only: bool = False
    group_id: int | None = None

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


def _text_span_start(x: int, text: str, justify: int) -> int:
    length = max(1, len(text))
    if justify == 1:
        return x - (length // 2)
    if justify == 2:
        return x - length + 1
    return x


def _pixel_span_start(pixel_x: int | None, tile_w: int | None, text: str, justify: int) -> int | None:
    if pixel_x is None or tile_w is None or tile_w <= 0:
        return None
    length = max(1, len(text))
    if justify == 1:
        return pixel_x - tile_w * (length - 1)
    if justify == 2:
        return pixel_x - tile_w * (length // 2)
    return pixel_x


@dataclass(frozen=True)
class _PlacedToken:
    text: str
    y: int
    x_start: int
    x_end: int
    pixel_left: int | None = None
    pixel_right: int | None = None
    pixel_top: int | None = None
    pixel_bottom: int | None = None


@dataclass(frozen=True)
class _RowSegment:
    y: int
    x_start: int
    x_end: int
    text: str
    pixel_left: int | None = None
    pixel_right: int | None = None
    pixel_top: int | None = None
    pixel_bottom: int | None = None


def _place_token(entry: TextEntry) -> _PlacedToken:
    x_start = _text_span_start(entry.x, entry.text, entry.justify)
    x_end = x_start + max(1, len(entry.text)) - 1

    return _PlacedToken(
        text=entry.text,
        y=entry.y,
        x_start=x_start,
        x_end=x_end,
    )


def _is_kept_text(text: str) -> bool:
    return any(char.isalpha() for char in text) or any(char in ".,!?->" for char in text)


def _collect_row_segments(entries: list[TextEntry]) -> list[_RowSegment]:
    max_intra_row_gap = 1

    rows: dict[int, list[_PlacedToken]] = {}
    for entry in entries:
        if _is_kept_text(entry.text):
            placed = _place_token(entry)
            rows.setdefault(placed.y, []).append(placed)

    row_segments: list[_RowSegment] = []
    for y in sorted(rows.keys()):
        tokens = sorted(rows[y], key=lambda token: token.x_start)
        cluster: list[str] = []
        x_start = tokens[0].x_start
        prev_x = tokens[0].x_start
        prev_len = 0
        x_end = x_start
        pixel_left = tokens[0].pixel_left
        pixel_right = tokens[0].pixel_right
        pixel_top = tokens[0].pixel_top
        pixel_bottom = tokens[0].pixel_bottom

        for token in tokens:
            gap = token.x_start - prev_x - prev_len
            if prev_len > 0 and gap > max_intra_row_gap:
                joined = _join_tokens(cluster)
                if any(char.isalpha() for char in joined):
                    row_segments.append(
                        _RowSegment(
                            y,
                            x_start,
                            x_end,
                            joined,
                            pixel_left,
                            pixel_right,
                            pixel_top,
                            pixel_bottom,
                        )
                    )
                cluster = []
                x_start = token.x_start
                x_end = token.x_end
                pixel_left = token.pixel_left
                pixel_right = token.pixel_right
                pixel_top = token.pixel_top
                pixel_bottom = token.pixel_bottom
            cluster.append(token.text)
            prev_x = token.x_start
            prev_len = len(token.text)
            x_end = max(x_end, token.x_end)
            if token.pixel_left is not None:
                pixel_left = token.pixel_left if pixel_left is None else min(pixel_left, token.pixel_left)
            if token.pixel_right is not None:
                pixel_right = token.pixel_right if pixel_right is None else max(pixel_right, token.pixel_right)
            if token.pixel_top is not None:
                pixel_top = token.pixel_top if pixel_top is None else min(pixel_top, token.pixel_top)
            if token.pixel_bottom is not None:
                pixel_bottom = token.pixel_bottom if pixel_bottom is None else max(pixel_bottom, token.pixel_bottom)

        if cluster:
            joined = _join_tokens(cluster)
            if any(char.isalpha() for char in joined):
                row_segments.append(
                    _RowSegment(
                        y,
                        x_start,
                        x_end,
                        joined,
                        pixel_left,
                        pixel_right,
                        pixel_top,
                        pixel_bottom,
                    )
                )

    return row_segments


def _span_from_row_segment(segment: _RowSegment) -> Span:
    return Span(
        segment.y,
        segment.x_start,
        segment.x_end,
        segment.pixel_left,
        segment.pixel_right,
        segment.pixel_top,
        segment.pixel_bottom,
    )


def _group_row_segments(row_segments: list[_RowSegment]) -> list[TextBlock]:
    max_x_margin_diff = 4
    sidebar_x_diff = 8

    if not row_segments:
        return []

    blocks: list[TextBlock] = []
    pending_segment = row_segments[0]
    pending_y = pending_segment.y
    pending_x = pending_segment.x_start
    pending_text = pending_segment.text
    pending_spans = [
        _span_from_row_segment(pending_segment)
    ]

    for segment in row_segments[1:]:
        y_gap = segment.y - pending_y
        x_margin_diff = abs(segment.x_start - pending_x)
        ends_sentence = pending_text[-1] in ".!?" if pending_text else True
        ends_with_arrow = pending_text.endswith("->")
        pending_has_internal_punct = any(char in ",." for char in pending_text[:-1])
        first_alpha = next((char for char in segment.text if char.isalpha()), None)
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
            pending_text = f"{pending_text} {segment.text}"
            pending_spans.append(_span_from_row_segment(segment))
            pending_y = segment.y
        elif y_gap == 0 and x_margin_diff > sidebar_x_diff:
            blocks.append(TextBlock(text=segment.text, spans=(_span_from_row_segment(segment),)))
        else:
            blocks.append(TextBlock(text=pending_text, spans=tuple(pending_spans)))
            pending_text = segment.text
            pending_spans = [_span_from_row_segment(segment)]
            pending_y = segment.y
            pending_x = segment.x_start

    blocks.append(TextBlock(text=pending_text, spans=tuple(pending_spans)))
    return blocks


def _build_rich_fallback_blocks(entries: list[TextEntry]) -> list[TextBlock]:
    rich_block_entries = {
        entry.group_id: entry
        for entry in entries
        if entry.kind == TEXT_KIND_RICH_BLOCK and entry.group_id is not None
    }
    if not rich_block_entries:
        return []

    rich_tokens_by_group: dict[int, list[TextEntry]] = {}
    for entry in entries:
        if entry.kind == TEXT_KIND_RICH_TOKEN and entry.group_id is not None:
            rich_tokens_by_group.setdefault(entry.group_id, []).append(entry)

    fallback_blocks: list[TextBlock] = []
    for group_id, rich_entry in rich_block_entries.items():
        segments = _collect_row_segments(rich_tokens_by_group.get(group_id, []))
        spans = tuple(_span_from_row_segment(segment) for segment in segments)
        if not spans:
            first_line = rich_entry.text.splitlines()[0] if rich_entry.text else rich_entry.text
            x_start = _text_span_start(rich_entry.x, first_line, rich_entry.justify)
            x_end = x_start + max(1, len(first_line)) - 1
            spans = (Span(rich_entry.y, x_start, x_end),)
        fallback_blocks.append(
            TextBlock(
                text=rich_entry.text,
                spans=spans,
                fallback_only=True,
                group_id=group_id,
            )
        )
    return fallback_blocks


def _group_text_blocks(entries: list[TextEntry]) -> list[TextBlock]:
    primary_entries = [entry for entry in entries if entry.kind != TEXT_KIND_RICH_BLOCK]
    primary_blocks = _group_row_segments(_collect_row_segments(primary_entries))
    fallback_blocks = _build_rich_fallback_blocks(entries)
    return primary_blocks + fallback_blocks


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


def _pixel_hover_rect_for_block(block: TextBlock, client_rect: QRect) -> QRect | None:
    pixel_spans = [
        span
        for span in block.spans
        if span.pixel_left is not None
        and span.pixel_right is not None
        and span.pixel_top is not None
        and span.pixel_bottom is not None
    ]
    if not pixel_spans:
        return None

    left = min(span.pixel_left for span in pixel_spans if span.pixel_left is not None)
    right = max(span.pixel_right for span in pixel_spans if span.pixel_right is not None)
    top = min(span.pixel_top for span in pixel_spans if span.pixel_top is not None)
    bottom = max(span.pixel_bottom for span in pixel_spans if span.pixel_bottom is not None)

    if left is None or right is None or top is None or bottom is None:
        return None

    return QRect(
        client_rect.left() + int(left) - 10,
        client_rect.top() + int(top) - 6,
        max(1, int(right - left) + 20),
        max(1, int(bottom - top) + 12),
    )


def _tile_hover_rect_for_block(
    block: TextBlock,
    client_rect: QRect,
    tile_size: tuple[int, int] | None,
    pad_x_tiles: int = 0,
    pad_y_tiles: int = 0,
) -> QRect | None:
    if not block.spans or tile_size is None:
        return None

    tile_w, tile_h = tile_size
    if tile_w <= 0 or tile_h <= 0:
        return None

    left_tile = min(span.x_start for span in block.spans) - pad_x_tiles
    right_tile = max(span.x_end for span in block.spans) + pad_x_tiles
    top_tile = min(span.y for span in block.spans) - pad_y_tiles
    bottom_tile = max(span.y for span in block.spans) + pad_y_tiles

    left = client_rect.left() + left_tile * tile_w
    top = client_rect.top() + top_tile * tile_h
    right = client_rect.left() + (right_tile + 1) * tile_w
    bottom = client_rect.top() + (bottom_tile + 1) * tile_h

    left = max(client_rect.left(), left)
    top = max(client_rect.top(), top)
    right = min(client_rect.right(), right)
    bottom = min(client_rect.bottom(), bottom)
    if right <= left or bottom <= top:
        return None

    return QRect(left, top, right - left, bottom - top)


def _frame_grid_size(frame: _TranslatedFrame) -> tuple[int, int]:
    max_col = max(
        span.x_end
        for block, _translation in frame
        for span in block.spans
    )
    max_row = max(
        span.y
        for block, _translation in frame
        for span in block.spans
    )
    cols = max(MIN_COLUMNS, max_col + 1)
    rows = max(MIN_ROWS, max_row + 1)
    return cols, rows


def _normalized_hover_rect_for_block(
    block: TextBlock,
    client_rect: QRect,
    cols: int,
    rows: int,
    pad_x_cells: float = 1.5,
    pad_y_cells: float = 0.8,
) -> QRect:
    cell_width = max(1.0, client_rect.width() / cols)
    cell_height = max(1.0, client_rect.height() / rows)

    left = min(span.x_start for span in block.spans) * cell_width + client_rect.left()
    right = (max(span.x_end for span in block.spans) + 1) * cell_width + client_rect.left()
    top = min(span.y for span in block.spans) * cell_height + client_rect.top()
    bottom = (max(span.y for span in block.spans) + 1) * cell_height + client_rect.top()

    return QRect(
        int(left - cell_width * pad_x_cells),
        int(top - cell_height * pad_y_cells),
        int(max(1.0, (right - left) + cell_width * pad_x_cells * 2.0)),
        int(max(1.0, (bottom - top) + cell_height * pad_y_cells * 2.0)),
    )


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
        self._label.setMaximumWidth(self._content_width)

    def show_translation(self, text: str, cursor_pos: QPoint, client_rect: QRect) -> None:
        metrics = QFontMetrics(self._label.font())
        longest_line_width = max(
            (metrics.horizontalAdvance(line) for line in text.splitlines()),
            default=metrics.horizontalAdvance(text),
        )
        target_width = min(
            self._content_width,
            max(MIN_TOOLTIP_WIDTH, longest_line_width + 28),
        )
        self._label.setFixedWidth(target_width)
        self._label.setText(text)
        self._label.adjustSize()
        self.adjustSize()

        if not self.isVisible():
            self.show()
        self.raise_()
        self._apply_click_through()

        native_width, native_height = self._native_size()

        preferred_x = cursor_pos.x() + CURSOR_OFFSET_X
        preferred_y = cursor_pos.y() + CURSOR_OFFSET_Y
        max_x = client_rect.right() - native_width
        max_y = client_rect.bottom() - native_height

        x = min(preferred_x, max_x)
        y = min(preferred_y, max_y)
        x = max(client_rect.left(), x)
        y = max(client_rect.top(), y)

        self._move_native(x, y)

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

    def _native_size(self) -> tuple[int, int]:
        try:
            left, top, right, bottom = win32gui.GetWindowRect(int(self.winId()))
            return max(1, right - left), max(1, bottom - top)
        except Exception:
            return max(1, self.width()), max(1, self.height())

    def _move_native(self, x: int, y: int) -> None:
        try:
            hwnd = int(self.winId())
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_TOPMOST,
                x,
                y,
                0,
                0,
                win32con.SWP_NOSIZE
                | win32con.SWP_NOACTIVATE
                | win32con.SWP_SHOWWINDOW,
            )
        except Exception as exc:
            logger.debug("Failed to move overlay window: %s", exc)
            self.move(x, y)


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
        self._mouse_tile: tuple[int, int] | None = None
        self._mouse_pixel: tuple[int, int] | None = None
        self._tile_size: tuple[int, int] | None = None
        self._screen_scale: tuple[float, float] | None = None
        self._last_sync_state: str | None = None
        self._last_frame_signature: tuple[int, str] | None = None

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

    def on_frame(self, entries: list[TextEntry]) -> None:
        blocks = _group_text_blocks(entries)
        if not blocks:
            self._log_sync_state("frame-empty")
            return

        self._connected = True
        tile_info = next(
            (
                entry
                for entry in entries
                if entry.tile_w is not None
                and entry.tile_h is not None
            ),
            None,
        )
        if tile_info is not None:
            tile_size = (tile_info.tile_w, tile_info.tile_h)
            if tile_size != self._tile_size:
                logger.debug("Tile size updated: %s", tile_size)
            self._tile_size = tile_size

        mouse_tile_info = next(
            (
                entry
                for entry in entries
                if entry.mouse_x is not None
                and entry.mouse_y is not None
            ),
            None,
        )
        if mouse_tile_info is not None:
            self._mouse_tile = (mouse_tile_info.mouse_x, mouse_tile_info.mouse_y)

        mouse_pixel_info = next(
            (
                entry
                for entry in entries
                if entry.mouse_pixel_x is not None
                and entry.mouse_pixel_y is not None
            ),
            None,
        )
        if mouse_pixel_info is not None:
            self._mouse_pixel = (mouse_pixel_info.mouse_pixel_x, mouse_pixel_info.mouse_pixel_y)
        signature = (len(blocks), blocks[0].text)
        if signature != self._last_frame_signature:
            self._last_frame_signature = signature
            logger.debug("Grouped %d text blocks; first=%r", len(blocks), blocks[0].text[:80])
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
            try:
                unique_texts: list[str] = []
                unique_indices: dict[str, int] = {}
                for block in blocks:
                    if block.text not in unique_indices:
                        unique_indices[block.text] = len(unique_texts)
                        unique_texts.append(block.text)
                translated_unique = self._translator.translate_batch(unique_texts)
                translated_by_text = {
                    text: translated_unique[index] for text, index in unique_indices.items()
                }
                frame = [(block, translated_by_text[block.text]) for block in blocks]
                try:
                    self._result_queue.put_nowait(frame)
                except queue.Full:
                    pass
            except Exception:
                logger.exception("Translation worker failed")

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
            preview = latest[0][1][:80] if latest else ""
            self._log_sync_state(f"frame-ready:{len(latest)}:{preview}")
            self._refresh_state()

    def _sync_overlay(self) -> None:
        hwnd = _find_df_window()
        self._last_hwnd = hwnd
        self._last_window_found = hwnd is not None

        if not self._overlay_enabled or hwnd is None:
            self._log_sync_state("overlay-disabled" if not self._overlay_enabled else "df-window-missing")
            self._overlay.hide()
            self._refresh_state()
            return

        client_rect = _client_rect_screen(hwnd)
        if client_rect is None:
            self._log_sync_state("client-rect-missing")
            self._overlay.hide()
            self._refresh_state()
            return

        cursor_pos = self._cursor_position(client_rect)
        if not client_rect.contains(cursor_pos):
            self._log_sync_state(
                f"cursor-outside:{cursor_pos.x()},{cursor_pos.y()} rect={client_rect.left()},{client_rect.top()}..{client_rect.right()},{client_rect.bottom()}"
            )
            self._overlay.hide()
            self._refresh_state()
            return

        self._update_cursor_calibration(cursor_pos, client_rect)

        if not self._current_frame:
            self._log_sync_state("cursor-inside-no-frame")
            self._overlay.hide()
            self._refresh_state()
            return

        hovered_translation = self._translation_for_cursor(cursor_pos, client_rect)
        if hovered_translation:
            self._log_sync_state(f"show:{hovered_translation[:80]}")
            self._overlay.show_translation(hovered_translation, cursor_pos, client_rect)
        else:
            self._log_sync_state("cursor-inside-no-translation")
            self._overlay.hide()

        self._refresh_state()

    def _translation_for_cursor(self, cursor_pos: QPoint, client_rect: QRect) -> str | None:
        if not self._current_frame:
            return None

        primary_frame = [
            (block, translation)
            for block, translation in self._current_frame
            if not block.fallback_only
        ]
        fallback_frame = [
            (block, translation)
            for block, translation in self._current_frame
            if block.fallback_only
        ]

        primary_translation = self._find_precise_translation(
            primary_frame,
            cursor_pos,
            client_rect,
        )
        if primary_translation is not None:
            return primary_translation

        return self._find_fallback_translation(
            fallback_frame,
            cursor_pos,
            client_rect,
        )

    def _find_precise_translation(
        self,
        frame: _TranslatedFrame,
        cursor_pos: QPoint,
        client_rect: QRect,
    ) -> str | None:
        if not frame:
            return None

        tile_cursor = self._cursor_tile_position(cursor_pos, client_rect)
        if tile_cursor is not None:
            mouse_x, mouse_y = tile_cursor
            for block, translation in frame:
                if block.matches(mouse_x, mouse_y):
                    return translation

            nearest_tile: tuple[float, str] | None = None
            for block, translation in frame:
                dx = min(
                    abs(mouse_x - span.x_start) if mouse_x < span.x_start else
                    abs(mouse_x - span.x_end) if mouse_x > span.x_end else
                    0
                    for span in block.spans
                )
                dy = min(abs(mouse_y - span.y) for span in block.spans)
                distance = dx + dy * 1.5
                if nearest_tile is None or distance < nearest_tile[0]:
                    nearest_tile = (distance, translation)

            if nearest_tile and nearest_tile[0] <= 2.5:
                return nearest_tile[1]
        elif self._mouse_tile is not None:
            mouse_x, mouse_y = self._mouse_tile
            for block, translation in frame:
                if block.matches(mouse_x, mouse_y):
                    return translation

        best_pixel: tuple[float, str] | None = None
        pixel_backed = False
        for block, translation in frame:
            hover_rect = _pixel_hover_rect_for_block(block, client_rect)
            if hover_rect is None:
                continue
            pixel_backed = True

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
            if best_pixel is None or distance < best_pixel[0]:
                best_pixel = (distance, translation)

        if pixel_backed:
            return best_pixel[1] if best_pixel and best_pixel[0] <= 40 else None

        cols, rows = _frame_grid_size(frame)
        cell_width = max(1.0, client_rect.width() / cols)

        best: tuple[float, str] | None = None
        for block, translation in frame:
            hover_rect = _normalized_hover_rect_for_block(block, client_rect, cols, rows)

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

    def _find_fallback_translation(
        self,
        frame: _TranslatedFrame,
        cursor_pos: QPoint,
        client_rect: QRect,
    ) -> str | None:
        if not frame:
            return None

        full_frame = self._current_frame if self._current_frame else frame
        cols, rows = _frame_grid_size(full_frame)
        for block, translation in frame:
            hover_rect = _normalized_hover_rect_for_block(
                block,
                client_rect,
                cols,
                rows,
                pad_x_cells=3.0,
                pad_y_cells=1.5,
            )
            if hover_rect.contains(cursor_pos):
                return translation
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

    def _log_sync_state(self, state: str) -> None:
        if state != self._last_sync_state:
            self._last_sync_state = state
            noisy_prefixes = (
                "frame-ready:",
                "show:",
                "cursor-inside-no-translation",
                "cursor-outside:",
            )
            if state.startswith(noisy_prefixes):
                logger.debug("Overlay state: %s", state)
            else:
                logger.info("Overlay state: %s", state)

    def _update_cursor_calibration(self, cursor_pos: QPoint, client_rect: QRect) -> None:
        if self._mouse_pixel is None:
            return

        pixel_x, pixel_y = self._mouse_pixel
        if pixel_x <= 0 or pixel_y <= 0:
            return

        rel_x = cursor_pos.x() - client_rect.left()
        rel_y = cursor_pos.y() - client_rect.top()
        if rel_x <= 0 or rel_y <= 0:
            return

        scale_x = rel_x / pixel_x
        scale_y = rel_y / pixel_y
        if not (0.5 <= scale_x <= 4.0 and 0.5 <= scale_y <= 4.0):
            return

        new_scale = (scale_x, scale_y)
        if self._screen_scale is None or any(
            abs(new - old) >= 0.05 for new, old in zip(new_scale, self._screen_scale)
        ):
            self._screen_scale = new_scale
            logger.debug("Cursor calibration updated: scale=(%.3f, %.3f)", scale_x, scale_y)

    def _cursor_position(self, client_rect: QRect) -> QPoint:
        cursor_x, cursor_y = win32gui.GetCursorPos()
        return QPoint(cursor_x, cursor_y)

    def _cursor_tile_position(self, cursor_pos: QPoint, client_rect: QRect) -> tuple[int, int] | None:
        if self._tile_size is None:
            return None

        tile_w, tile_h = self._tile_size
        if tile_w <= 0 or tile_h <= 0:
            return None

        rel_x = cursor_pos.x() - client_rect.left()
        rel_y = cursor_pos.y() - client_rect.top()
        if rel_x < 0 or rel_y < 0:
            return None

        if self._screen_scale is not None:
            scale_x, scale_y = self._screen_scale
            if scale_x > 0 and scale_y > 0:
                internal_x = rel_x / scale_x
                internal_y = rel_y / scale_y
                return (int(internal_x // tile_w), int(internal_y // tile_h))

        return (int(rel_x // tile_w), int(rel_y // tile_h))
