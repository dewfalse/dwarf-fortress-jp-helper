"""Cursor-following overlay UI for Dwarf Fortress."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
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

from config import load_config
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
LOADING_FRAME_INTERVAL_SECONDS = 0.12
LOADING_INDENT_SEQUENCE = (0, 1, 2, 3, 4, 5, 6, 5, 4, 3, 2, 1)
MIN_COLUMNS = 80
MIN_ROWS = 25
TOOLTIP_MARGIN = 12
MIN_TOOLTIP_WIDTH = 180
COMPACT_TOOLTIP_MARGIN = 0
COMPACT_MIN_TOOLTIP_WIDTH = 72
ALL_TEXT_GAP = 8
ALL_TEXT_CLUSTER_MARGIN = 6
ALL_TEXT_COLUMN_MERGE_GAP = 28
ALL_TEXT_MAX_CLUSTER_ITEMS = 3
DEFAULT_ALL_TEXT_VERTICAL_SHIFT_RATIO = 0.85
ALL_TEXT_MAX_SOURCE_VERTICAL_GAP = 48
ALL_TEXT_MAX_MERGE_SOURCE_TEXT_LENGTH = 80
ALL_TEXT_MIN_VERTICAL_SHIFT = 4
DEFAULT_TOOLTIP_OPACITY = 0.78
DEFAULT_TRANSLATION_FONT_SIZE = 12.0


class OverlayMode(Enum):
    HOVER = "hover"
    ALL_TEXT = "all-text"
    OFF = "off"

    @property
    def display_name(self) -> str:
        if self is OverlayMode.HOVER:
            return "Hover"
        if self is OverlayMode.ALL_TEXT:
            return "All text"
        return "Off"


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


def _blocks_overlap(left: TextBlock, right: TextBlock) -> bool:
    for left_span in left.spans:
        for right_span in right.spans:
            if left_span.y != right_span.y:
                continue
            if left_span.x_start <= right_span.x_end and right_span.x_start <= left_span.x_end:
                return True
    return False


def _clamp_overlay_rect(x: int, y: int, width: int, height: int, client_rect: QRect) -> QRect:
    max_x = max(client_rect.left(), client_rect.right() - width)
    max_y = max(client_rect.top(), client_rect.bottom() - height)
    clamped_x = max(client_rect.left(), min(x, max_x))
    clamped_y = max(client_rect.top(), min(y, max_y))
    return QRect(clamped_x, clamped_y, max(1, width), max(1, height))


def _rect_union(rects: list[QRect]) -> QRect | None:
    if not rects:
        return None
    merged = QRect(rects[0])
    for rect in rects[1:]:
        merged = merged.united(rect)
    return merged


def _vertical_rects_touch_or_overlap(top_rect: QRect, bottom_rect: QRect, margin: int = 0) -> bool:
    top = min(top_rect.top(), bottom_rect.top())
    bottom = max(top_rect.bottom(), bottom_rect.bottom())
    combined_height = top_rect.height() + bottom_rect.height() + margin * 2
    return (bottom - top) <= combined_height


def _rects_share_column(left: QRect, right: QRect, gap: int = ALL_TEXT_COLUMN_MERGE_GAP) -> bool:
    left_start = left.left() - gap
    left_end = left.right() + gap
    right_start = right.left() - gap
    right_end = right.right() + gap
    return left_start <= right_end and right_start <= left_end


def _source_rects_stack_vertically(top_rect: QRect, bottom_rect: QRect) -> bool:
    top_center = top_rect.center().y()
    bottom_center = bottom_rect.center().y()
    min_required = max(6, min(top_rect.height(), bottom_rect.height()) // 2)
    return abs(top_center - bottom_center) >= min_required


def _clamp_tooltip_opacity(value: float) -> float:
    return max(0.05, min(1.0, value))


def _clamp_all_text_vertical_shift_ratio(value: float) -> float:
    return max(0.1, min(2.0, value))


def _clamp_translation_font_size(value: float) -> float:
    return max(8.0, min(24.0, value))


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


def _normalize_toggle_hotkey(name: str) -> str:
    normalized = str(name).strip().lower()
    if normalized in {"ctrl", "shift", "alt"}:
        return normalized
    return "ctrl"


def _toggle_hotkey_display_name(name: str) -> str:
    normalized = _normalize_toggle_hotkey(name)
    if normalized == "shift":
        return "Shift"
    if normalized == "alt":
        return "Alt"
    return "Ctrl"


def _is_toggle_hotkey_down(name: str) -> bool:
    normalized = _normalize_toggle_hotkey(name)
    if normalized == "shift":
        return bool(
            win32api.GetAsyncKeyState(win32con.VK_LSHIFT) & 0x8000
            or win32api.GetAsyncKeyState(win32con.VK_RSHIFT) & 0x8000
        )
    if normalized == "alt":
        return bool(
            win32api.GetAsyncKeyState(win32con.VK_LMENU) & 0x8000
            or win32api.GetAsyncKeyState(win32con.VK_RMENU) & 0x8000
        )
    return _is_ctrl_down()


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
    def __init__(
        self,
        tooltip_opacity: float = DEFAULT_TOOLTIP_OPACITY,
        compact: bool = False,
        translation_font_size: float = DEFAULT_TRANSLATION_FONT_SIZE,
    ) -> None:
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

        self._compact = compact
        self._outer_margin = COMPACT_TOOLTIP_MARGIN if compact else TOOLTIP_MARGIN
        self._min_width = COMPACT_MIN_TOOLTIP_WIDTH if compact else MIN_TOOLTIP_WIDTH
        self._padding_y = 6 if compact else 10
        self._padding_x = 8 if compact else 12
        self._border_radius = 7 if compact else 9
        self._width_padding = self._padding_x * 2 + (14 if compact else 6)
        self._translation_font_size = _clamp_translation_font_size(translation_font_size)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            self._outer_margin,
            self._outer_margin,
            self._outer_margin,
            self._outer_margin,
        )

        self._label = QLabel()
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        label_font = QFont()
        if hasattr(label_font, "setFamilies"):
            label_font.setFamilies(["Meiryo UI", "Yu Gothic UI", "MS UI Gothic"])
        else:
            label_font.setFamily("Meiryo UI")
        label_font.setPixelSize(max(1, int(round(self._translation_font_size))))
        self._label.setFont(label_font)
        layout.addWidget(self._label)

        metrics = QFontMetrics(self._label.font())
        self._content_width = max(420, metrics.averageCharWidth() * 50)
        self._label.setMaximumWidth(self._content_width)
        self._tooltip_opacity = _clamp_tooltip_opacity(tooltip_opacity)
        self._apply_stylesheet()
        self._last_position: tuple[int, int] | None = None
        self._click_through_ready = False
        self._prepared_text: str | None = None
        self._prepared_width: int | None = None
        self._prepared_word_wrap: bool | None = None

    def set_tooltip_opacity(self, tooltip_opacity: float) -> None:
        clamped = _clamp_tooltip_opacity(tooltip_opacity)
        if abs(clamped - self._tooltip_opacity) < 0.001:
            return
        self._tooltip_opacity = clamped
        self._apply_stylesheet()

    def _apply_stylesheet(self) -> None:
        background_alpha = max(12, min(255, int(round(255 * self._tooltip_opacity))))
        border_alpha = max(12, min(80, int(round(48 * self._tooltip_opacity))))
        self._label.setStyleSheet(
            f"""
            QLabel {{
                color: #f3f3f3;
                background-color: rgba(16, 16, 16, {background_alpha});
                border: 1px solid rgba(255, 255, 255, {border_alpha});
                border-radius: {self._border_radius}px;
                padding: {self._padding_y}px {self._padding_x}px;
            }}
            """
        )

    def show_translation(self, text: str, cursor_pos: QPoint, client_rect: QRect) -> None:
        self._prepare_text(text)

        preferred_x = cursor_pos.x() + CURSOR_OFFSET_X
        preferred_y = cursor_pos.y() + CURSOR_OFFSET_Y
        self._show_at(preferred_x, preferred_y, client_rect)

    def show_translation_near_rect(self, text: str, target_rect: QRect, client_rect: QRect) -> None:
        self._prepare_text(text)

        native_width, _native_height = self._native_size()
        preferred_x = target_rect.right() + 8
        preferred_y = max(client_rect.top(), target_rect.top() - 4)
        fallback_x = target_rect.left() - native_width - 8
        self._show_at(preferred_x, preferred_y, client_rect, fallback_x=fallback_x)

    def prepare_translation(self, text: str) -> tuple[int, int]:
        self._prepare_text(text)
        return max(1, self.width()), max(1, self.height())

    def show_prepared_at(self, x: int, y: int, client_rect: QRect) -> QRect:
        return self._show_at(x, y, client_rect)

    def _prepare_text(self, text: str) -> None:
        metrics = QFontMetrics(self._label.font())
        text_lines = text.splitlines()
        longest_line_width = max(
            (metrics.horizontalAdvance(line) for line in text_lines),
            default=metrics.horizontalAdvance(text),
        )
        single_line_text = len(text_lines) <= 1
        disable_wrap_for_single_line = (
            self._compact
            and single_line_text
            and (longest_line_width + self._width_padding + 10) <= self._content_width
        )
        target_width = min(
            self._content_width,
            max(
                self._min_width,
                longest_line_width + self._width_padding + (10 if disable_wrap_for_single_line else 0),
            ),
        )
        if (
            text == self._prepared_text
            and target_width == self._prepared_width
            and disable_wrap_for_single_line == self._prepared_word_wrap
        ):
            return
        self._prepared_text = text
        self._prepared_width = target_width
        self._prepared_word_wrap = disable_wrap_for_single_line
        self._label.setWordWrap(not disable_wrap_for_single_line)
        self._label.setFixedWidth(target_width)
        self._label.setText(text)
        self._label.adjustSize()
        self.adjustSize()

    def _show_at(
        self,
        preferred_x: int,
        preferred_y: int,
        client_rect: QRect,
        fallback_x: int | None = None,
    ) -> QRect:
        if not self.isVisible():
            self.show()
            self.raise_()
        if not self._click_through_ready:
            self._apply_click_through()
            self._click_through_ready = True

        native_width, native_height = self._native_size()
        max_x = client_rect.right() - native_width
        max_y = client_rect.bottom() - native_height

        x = preferred_x
        if x > max_x:
            if fallback_x is not None and fallback_x >= client_rect.left():
                x = fallback_x
            else:
                x = max_x
        y = min(preferred_y, max_y)
        x = max(client_rect.left(), x)
        y = max(client_rect.top(), y)

        if self._last_position != (x, y):
            self._move_native(x, y)
            self._last_position = (x, y)
        return QRect(x, y, native_width, native_height)

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
        self._config = load_config()
        self._tooltip_opacity = _clamp_tooltip_opacity(self._config.tooltip_opacity)
        self._all_text_vertical_shift_ratio = _clamp_all_text_vertical_shift_ratio(
            getattr(self._config, "all_text_vertical_shift_ratio", DEFAULT_ALL_TEXT_VERTICAL_SHIFT_RATIO)
        )
        self._translation_font_size = _clamp_translation_font_size(
            getattr(self._config, "translation_font_size", DEFAULT_TRANSLATION_FONT_SIZE)
        )
        self._toggle_hotkey = _normalize_toggle_hotkey(getattr(self._config, "toggle_hotkey", "ctrl"))
        self._toggle_hotkey_label = _toggle_hotkey_display_name(self._toggle_hotkey)
        self._translator = Translator()
        self._reader = PipeReader(on_frame=self.on_frame)
        self._overlay = CursorOverlay(
            self._tooltip_opacity,
            translation_font_size=self._translation_font_size,
        )
        self._all_text_overlays: list[CursorOverlay] = []
        self._raw_queue: queue.Queue[list[TextBlock]] = queue.Queue(maxsize=4)
        self._result_queue: queue.Queue[_TranslatedFrame] = queue.Queue(maxsize=4)
        self._overlay_mode = OverlayMode.HOVER
        self._last_active_mode = OverlayMode.HOVER
        self._connected = False
        self._ctrl_was_down = False
        self._last_ctrl_toggle = 0.0
        self._last_window_found = False
        self._last_hwnd: int | None = None
        self._current_frame: _TranslatedFrame = []
        self._source_frame: _TranslatedFrame = []
        self._mouse_tile: tuple[int, int] | None = None
        self._mouse_pixel: tuple[int, int] | None = None
        self._tile_size: tuple[int, int] | None = None
        self._screen_scale: tuple[float, float] | None = None
        self._loading_text_key: str | None = None
        self._loading_started_at = 0.0
        self._last_sync_state: str | None = None
        self._last_frame_signature: tuple[int, str] | None = None
        self._last_enqueued_signature: tuple[str, ...] | None = None
        self._last_all_text_render_signature: tuple[object, ...] | None = None

        self._tray_icon_on = _build_tray_icon(QColor(255, 255, 255))
        self._tray_icon_off = _build_tray_icon(QColor(140, 140, 140))
        self._tray = self._create_tray()

        self._start_translation_worker()
        self._start_timers()
        self._reader.start()
        self._refresh_state()

    def shutdown(self) -> None:
        self._reader.stop()
        self._hide_all_overlays()
        self._tray.hide()

    def on_frame(self, entries: list[TextEntry]) -> None:
        blocks = _group_text_blocks(entries)
        if not blocks:
            self._source_frame = []
            self._log_sync_state("frame-empty")
            return

        self._connected = True
        self._source_frame = [(block, block.text) for block in blocks]
        self._translator.collect_detected_texts([block.text for block in blocks])
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
        enqueue_signature = tuple(block.text for block in blocks)
        if enqueue_signature != self._last_enqueued_signature:
            try:
                self._raw_queue.put_nowait(blocks)
                self._last_enqueued_signature = enqueue_signature
            except queue.Full:
                pass

    def toggle_overlay(self, source: str) -> None:
        if self._overlay_mode is OverlayMode.OFF:
            self._set_overlay_mode(self._last_active_mode, source)
        else:
            self._set_overlay_mode(OverlayMode.OFF, source)

    def cycle_overlay_mode(self, source: str) -> None:
        next_mode = {
            OverlayMode.HOVER: OverlayMode.ALL_TEXT,
            OverlayMode.ALL_TEXT: OverlayMode.OFF,
            OverlayMode.OFF: OverlayMode.HOVER,
        }[self._overlay_mode]
        self._set_overlay_mode(next_mode, source)

    def _set_overlay_mode(self, mode: OverlayMode, source: str) -> None:
        previous = self._overlay_mode
        self._overlay_mode = mode
        if mode is not OverlayMode.OFF:
            self._last_active_mode = mode
        logger.info(
            "Overlay mode changed %s -> %s by %s",
            previous.value,
            mode.value,
            source,
        )
        if mode is not OverlayMode.ALL_TEXT:
            self._last_all_text_render_signature = None
        if mode is OverlayMode.OFF:
            self._hide_all_overlays()
        self._refresh_state()

    def _create_tray(self) -> QSystemTrayIcon:
        tray = QSystemTrayIcon(self._tray_icon_on, self._app)
        tray.setToolTip("DFJP overlay: Hover")
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

        if self._overlay_mode is OverlayMode.OFF or hwnd is None:
            self._log_sync_state("overlay-disabled" if self._overlay_mode is OverlayMode.OFF else "df-window-missing")
            self._hide_all_overlays()
            self._refresh_state()
            return

        client_rect = _client_rect_screen(hwnd)
        if client_rect is None:
            self._log_sync_state("client-rect-missing")
            self._hide_all_overlays()
            self._refresh_state()
            return

        cursor_pos = self._cursor_position(client_rect)
        if not client_rect.contains(cursor_pos):
            self._log_sync_state(
                f"cursor-outside:{cursor_pos.x()},{cursor_pos.y()} rect={client_rect.left()},{client_rect.top()}..{client_rect.right()},{client_rect.bottom()}"
            )
            self._hide_all_overlays()
            self._refresh_state()
            return

        self._update_cursor_calibration(cursor_pos, client_rect)

        if not self._source_frame:
            self._log_sync_state("cursor-inside-no-frame")
            self._hide_all_overlays()
            self._refresh_state()
            return

        if self._overlay_mode is OverlayMode.ALL_TEXT:
            self._sync_all_text_overlays(client_rect)
        else:
            self._sync_hover_overlay(cursor_pos, client_rect)

        self._refresh_state()

    def _sync_hover_overlay(self, cursor_pos: QPoint, client_rect: QRect) -> None:
        self._hide_all_text_overlays()
        self._last_all_text_render_signature = None

        hovered_text = self._source_text_for_cursor(cursor_pos, client_rect)
        if hovered_text:
            overlay_text, is_loading = self._overlay_text_for_source_text(hovered_text)
            if is_loading:
                if hovered_text != self._loading_text_key:
                    self._loading_text_key = hovered_text
                    self._loading_started_at = time.monotonic()
                    overlay_text = self._loading_indicator_text()
                self._log_sync_state("show-loading")
            else:
                self._loading_text_key = None
                self._log_sync_state(f"show:{overlay_text[:80]}")
            self._overlay.show_translation(overlay_text, cursor_pos, client_rect)
        else:
            self._loading_text_key = None
            self._log_sync_state("cursor-inside-no-translation")
            self._overlay.hide()

    def _sync_all_text_overlays(self, client_rect: QRect) -> None:
        display_frame = self._display_source_frame()

        if not display_frame:
            self._hide_all_overlays()
            self._last_all_text_render_signature = None
            self._log_sync_state("all-text-no-frame")
            return

        rendered_blocks: list[tuple[TextBlock, str, bool]] = []
        has_loading = False
        for block, _source_text in display_frame:
            if not self._translator.is_active:
                overlay_text = block.text
                is_loading = False
            else:
                cached_translation = self._translator.get_cached_translation(block.text)
                if cached_translation is None:
                    overlay_text = "..."
                    is_loading = True
                else:
                    overlay_text = cached_translation
                    is_loading = False
            rendered_blocks.append((block, overlay_text, is_loading))
            has_loading = has_loading or is_loading

        if has_loading:
            if self._loading_text_key != "__all__":
                self._loading_text_key = "__all__"
                self._loading_started_at = time.monotonic()
        else:
            self._loading_text_key = None

        self._overlay.hide()
        self._ensure_all_text_overlays(len(rendered_blocks))

        cluster_items: list[dict[str, object]] = []
        for block, overlay_text, is_loading in sorted(
            rendered_blocks,
            key=lambda item: (
                min(span.y for span in item[0].spans) if item[0].spans else 0,
                min(span.x_start for span in item[0].spans) if item[0].spans else 0,
                item[0].text,
            ),
        ):
            target_rect = self._block_display_rect(block, client_rect)
            source_rect = self._block_source_rect(block, client_rect)
            if target_rect is None:
                continue
            if source_rect is None:
                source_rect = QRect(target_rect)
            cluster_items.append(
                {
                    "target_rect": target_rect,
                    "texts": [overlay_text],
                    "sort_key": (
                        min(span.y for span in block.spans) if block.spans else 0,
                        min(span.x_start for span in block.spans) if block.spans else 0,
                        block.text,
                    ),
                    "source_rect": source_rect,
                    "loading": is_loading,
                    "item_count": 1,
                    "max_source_text_length": len(block.text),
                }
            )

        render_signature = self._build_all_text_render_signature(cluster_items, client_rect)
        if render_signature == self._last_all_text_render_signature:
            return
        self._last_all_text_render_signature = render_signature

        final_clusters = self._merge_all_text_clusters(cluster_items, client_rect)
        shown = 0
        placed_cluster_rects: list[QRect] = []
        for cluster in final_clusters:
            overlay = self._all_text_overlays[shown]
            cluster_text = str(cluster["text"])
            prepared_width, prepared_height = overlay.prepare_translation(cluster_text)
            preferred_rect = self._overlay_rect_for_source_rect(
                cluster["source_rect"],
                prepared_width,
                prepared_height,
                client_rect,
            )
            cluster_rect = self._stagger_all_text_cluster_rect(
                overlay,
                preferred_rect,
                placed_cluster_rects,
                client_rect,
            )
            placed_cluster_rects.append(cluster_rect)
            shown += 1

        for overlay in self._all_text_overlays[shown:]:
            overlay.hide()

        if has_loading:
            self._log_sync_state(f"show-all-loading:{shown}")
        else:
            self._log_sync_state(f"show-all:{shown}")

    def _merge_all_text_clusters(
        self,
        cluster_items: list[dict[str, object]],
        client_rect: QRect,
    ) -> list[dict[str, object]]:
        if not cluster_items:
            return []

        items = list(cluster_items)
        while True:
            measured_items: list[dict[str, object]] = []
            ordered_items = sorted(
                items,
                key=lambda item: (
                    item["source_rect"].left(),
                    item["source_rect"].top(),
                    item["sort_key"],
                ),
            )
            for index, item in enumerate(ordered_items):
                overlay = self._all_text_overlays[index]
                text = self._combine_cluster_texts(item["texts"])
                overlay.prepare_translation(text)
                target_rect = QRect(item["target_rect"])
                source_rect = QRect(item.get("source_rect", target_rect))
                preferred_rect = self._overlay_rect_for_source_rect(
                    source_rect,
                    overlay.width(),
                    overlay.height(),
                    client_rect,
                )
                measured_items.append(
                    {
                        "target_rect": target_rect,
                        "source_rect": source_rect,
                        "texts": list(item["texts"]),
                        "sort_key": item["sort_key"],
                        "text": text,
                        "preferred_rect": preferred_rect,
                        "item_count": int(item.get("item_count", 1)),
                        "max_source_text_length": int(item.get("max_source_text_length", len(text))),
                    }
                )

            merged_items, did_merge = self._merge_adjacent_all_text_clusters(measured_items)
            if not did_merge:
                return sorted(measured_items, key=lambda item: item["sort_key"])
            items = merged_items

    def _build_all_text_render_signature(
        self,
        cluster_items: list[dict[str, object]],
        client_rect: QRect,
    ) -> tuple[object, ...]:
        items_signature = tuple(
            (
                tuple(item["texts"]),
                item["loading"],
                item["sort_key"],
                (
                    item["source_rect"].left(),
                    item["source_rect"].top(),
                    item["source_rect"].width(),
                    item["source_rect"].height(),
                ),
            )
            for item in cluster_items
        )
        return (
            client_rect.left(),
            client_rect.top(),
            client_rect.width(),
            client_rect.height(),
            items_signature,
        )

    def _merge_adjacent_all_text_clusters(
        self,
        measured_items: list[dict[str, object]],
    ) -> tuple[list[dict[str, object]], bool]:
        if not measured_items:
            return [], False

        merged_items: list[dict[str, object]] = []
        current = self._strip_cluster_measurement(measured_items[0])
        did_merge = False

        for next_item in measured_items[1:]:
            if self._can_merge_all_text_clusters(current, next_item):
                current = self._merge_cluster_pair(current, next_item)
                did_merge = True
            else:
                merged_items.append(current)
                current = self._strip_cluster_measurement(next_item)

        merged_items.append(current)
        return merged_items, did_merge

    def _strip_cluster_measurement(self, item: dict[str, object]) -> dict[str, object]:
        return {
            "target_rect": QRect(item["target_rect"]),
            "source_rect": QRect(item["source_rect"]),
            "texts": list(item["texts"]),
            "sort_key": item["sort_key"],
            "item_count": int(item.get("item_count", len(item["texts"]))),
            "max_source_text_length": int(item.get("max_source_text_length", 0)),
        }

    def _can_merge_all_text_clusters(
        self,
        current: dict[str, object],
        next_item: dict[str, object],
    ) -> bool:
        current_count = int(current.get("item_count", 1))
        next_count = int(next_item.get("item_count", 1))
        if current_count + next_count > ALL_TEXT_MAX_CLUSTER_ITEMS:
            return False

        current_max_len = int(current.get("max_source_text_length", 0))
        next_max_len = int(next_item.get("max_source_text_length", 0))
        if (
            current_max_len > ALL_TEXT_MAX_MERGE_SOURCE_TEXT_LENGTH
            or next_max_len > ALL_TEXT_MAX_MERGE_SOURCE_TEXT_LENGTH
        ):
            return False

        current_rect = current["preferred_rect"] if "preferred_rect" in current else current["target_rect"]
        next_rect = next_item["preferred_rect"] if "preferred_rect" in next_item else next_item["target_rect"]
        current_source_rect = current["source_rect"]
        next_source_rect = next_item["source_rect"]

        vertical_gap = 0
        if current_source_rect.bottom() < next_source_rect.top():
            vertical_gap = next_source_rect.top() - current_source_rect.bottom()
        elif next_source_rect.bottom() < current_source_rect.top():
            vertical_gap = current_source_rect.top() - next_source_rect.bottom()

        return (
            _vertical_rects_touch_or_overlap(current_rect, next_rect, ALL_TEXT_CLUSTER_MARGIN)
            and _rects_share_column(current_source_rect, next_source_rect)
            and _source_rects_stack_vertically(current_source_rect, next_source_rect)
            and vertical_gap <= ALL_TEXT_MAX_SOURCE_VERTICAL_GAP
        )

    def _merge_cluster_pair(
        self,
        current: dict[str, object],
        next_item: dict[str, object],
    ) -> dict[str, object]:
        target_rect = _rect_union([QRect(current["target_rect"]), QRect(next_item["target_rect"])])
        source_rect = _rect_union([QRect(current["source_rect"]), QRect(next_item["source_rect"])])
        merged_texts: list[str] = []
        for text in list(current["texts"]) + list(next_item["texts"]):
            if text not in merged_texts:
                merged_texts.append(text)

        return {
            "target_rect": target_rect if target_rect is not None else QRect(current["target_rect"]),
            "source_rect": source_rect if source_rect is not None else QRect(current["source_rect"]),
            "texts": merged_texts,
            "sort_key": min(current["sort_key"], next_item["sort_key"]),
            "item_count": int(current.get("item_count", 1)) + int(next_item.get("item_count", 1)),
            "max_source_text_length": max(
                int(current.get("max_source_text_length", 0)),
                int(next_item.get("max_source_text_length", 0)),
            ),
        }

    def _combine_cluster_texts(self, texts: list[str]) -> str:
        unique_texts: list[str] = []
        for text in texts:
            if text and text not in unique_texts:
                unique_texts.append(str(text))
        return "\n\n".join(unique_texts)

    def _overlay_rect_for_source_rect(
        self,
        source_rect: QRect,
        width: int,
        height: int,
        client_rect: QRect,
    ) -> QRect:
        preferred_x = source_rect.left() + (source_rect.width() - width) // 2
        return _clamp_overlay_rect(
            preferred_x,
            source_rect.top(),
            width,
            height,
            client_rect,
        )

    def _stagger_all_text_cluster_rect(
        self,
        overlay: CursorOverlay,
        preferred_rect: QRect,
        placed_rects: list[QRect],
        client_rect: QRect,
    ) -> QRect:
        rect = QRect(preferred_rect)
        actual_rect = QRect(rect)
        for _ in range(8):
            actual_rect = overlay.show_prepared_at(rect.x(), rect.y(), client_rect)
            overlapping = [
                other
                for other in placed_rects
                if other.intersects(actual_rect)
                and other.left() < actual_rect.right()
                and actual_rect.left() < other.right()
            ]
            if not overlapping:
                return actual_rect

            next_y = max(
                other.top() + max(
                    ALL_TEXT_GAP,
                    ALL_TEXT_MIN_VERTICAL_SHIFT,
                    int(other.height() * self._all_text_vertical_shift_ratio),
                )
                for other in overlapping
            )
            shifted = _clamp_overlay_rect(
                actual_rect.x(),
                next_y,
                actual_rect.width(),
                actual_rect.height(),
                client_rect,
            )
            if shifted == rect:
                return actual_rect
            rect = shifted

        return actual_rect

    def _display_source_frame(self) -> list[tuple[TextBlock, str]]:
        if not self._source_frame:
            return []

        fallback_blocks = [
            block
            for block, _text in self._source_frame
            if block.fallback_only
        ]
        display_frame: list[tuple[TextBlock, str]] = []
        for block, text in self._source_frame:
            if not block.fallback_only and any(
                _blocks_overlap(block, fallback_block) for fallback_block in fallback_blocks
            ):
                continue
            display_frame.append((block, text))
        return display_frame

    def _source_text_for_cursor(self, cursor_pos: QPoint, client_rect: QRect) -> str | None:
        if not self._source_frame:
            return None

        primary_frame = [
            (block, text)
            for block, text in self._source_frame
            if not block.fallback_only
        ]
        fallback_frame = [
            (block, text)
            for block, text in self._source_frame
            if block.fallback_only
        ]

        primary_text = self._find_precise_translation(
            primary_frame,
            cursor_pos,
            client_rect,
        )
        if primary_text is not None:
            return primary_text

        return self._find_fallback_translation(
            fallback_frame,
            cursor_pos,
            client_rect,
        )

    def _overlay_text_for_source_text(self, source_text: str) -> tuple[str, bool]:
        if not self._translator.is_active:
            return source_text, False

        cached_translation = self._translator.get_cached_translation(source_text)
        if cached_translation is not None:
            return cached_translation, False

        return self._loading_indicator_text(), True

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

        full_frame = self._source_frame if self._source_frame else frame
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

    def _loading_indicator_text(self) -> str:
        elapsed = max(0.0, time.monotonic() - self._loading_started_at)
        phase = int(elapsed / LOADING_FRAME_INTERVAL_SECONDS) % len(LOADING_INDENT_SEQUENCE)
        return " " * LOADING_INDENT_SEQUENCE[phase] + "..."

    def _block_display_rect(self, block: TextBlock, client_rect: QRect) -> QRect | None:
        pixel_rect = _pixel_hover_rect_for_block(block, client_rect)
        if pixel_rect is not None:
            return pixel_rect

        tile_rect = _tile_hover_rect_for_block(block, client_rect, self._tile_size)
        if tile_rect is not None:
            return tile_rect

        full_frame = self._source_frame if self._source_frame else [(block, block.text)]
        cols, rows = _frame_grid_size(full_frame)
        return _normalized_hover_rect_for_block(
            block,
            client_rect,
            cols,
            rows,
            pad_x_cells=0.2,
            pad_y_cells=0.2,
        )

    def _block_source_rect(self, block: TextBlock, client_rect: QRect) -> QRect | None:
        pixel_spans = [
            span
            for span in block.spans
            if span.pixel_left is not None
            and span.pixel_right is not None
            and span.pixel_top is not None
            and span.pixel_bottom is not None
        ]
        if pixel_spans:
            left = min(span.pixel_left for span in pixel_spans if span.pixel_left is not None)
            right = max(span.pixel_right for span in pixel_spans if span.pixel_right is not None)
            top = min(span.pixel_top for span in pixel_spans if span.pixel_top is not None)
            bottom = max(span.pixel_bottom for span in pixel_spans if span.pixel_bottom is not None)
            return QRect(
                client_rect.left() + int(left),
                client_rect.top() + int(top),
                max(1, int(right - left)),
                max(1, int(bottom - top)),
            )

        if self._tile_size is not None and block.spans:
            tile_w, tile_h = self._tile_size
            left_tile = min(span.x_start for span in block.spans)
            right_tile = max(span.x_end for span in block.spans)
            top_tile = min(span.y for span in block.spans)
            bottom_tile = max(span.y for span in block.spans)
            return QRect(
                client_rect.left() + left_tile * tile_w,
                client_rect.top() + top_tile * tile_h,
                max(1, (right_tile - left_tile + 1) * tile_w),
                max(1, (bottom_tile - top_tile + 1) * tile_h),
            )

        if block.spans:
            full_frame = self._source_frame if self._source_frame else [(block, block.text)]
            cols, rows = _frame_grid_size(full_frame)
            return _normalized_hover_rect_for_block(
                block,
                client_rect,
                cols,
                rows,
                pad_x_cells=0.0,
                pad_y_cells=0.0,
            )

        return None

    def _ensure_all_text_overlays(self, count: int) -> None:
        while len(self._all_text_overlays) < count:
            self._all_text_overlays.append(
                CursorOverlay(
                    self._tooltip_opacity,
                    compact=True,
                    translation_font_size=self._translation_font_size,
                )
            )

    def _hide_all_text_overlays(self) -> None:
        for overlay in self._all_text_overlays:
            overlay.hide()

    def _hide_all_overlays(self) -> None:
        self._overlay.hide()
        self._hide_all_text_overlays()
        self._last_all_text_render_signature = None

    def _poll_ctrl_key(self) -> None:
        ctrl_down = _is_toggle_hotkey_down(self._toggle_hotkey)
        now = time.monotonic()
        if (
            ctrl_down
            and not self._ctrl_was_down
            and now - self._last_ctrl_toggle >= CTRL_TOGGLE_COOLDOWN_SECONDS
        ):
            self.cycle_overlay_mode(self._toggle_hotkey_label)
            self._last_ctrl_toggle = now
        self._ctrl_was_down = ctrl_down

    def _refresh_state(self) -> None:
        state = self._overlay_mode.display_name
        window_state = "DF detected" if self._last_window_found else "waiting for Dwarf Fortress"
        connection = "connected" if self._connected else "waiting for text"

        self._toggle_action.setText("Turn overlay off" if self._overlay_mode is not OverlayMode.OFF else "Turn overlay on")
        self._tray.setIcon(self._tray_icon_on if self._overlay_mode is not OverlayMode.OFF else self._tray_icon_off)
        self._tray.setToolTip(
            f"DFJP overlay: {state}\n{self._toggle_hotkey_label}: Hover -> All text -> Off\n{window_state}\n{connection}\n{self._translator.engine_name}"
        )

        if self._overlay_mode is OverlayMode.OFF:
            self._hide_all_overlays()

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
