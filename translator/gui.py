"""PyQt6 製の翻訳表示ウィンドウ。"""

import queue
import threading
import logging

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QStatusBar,
    QTableWidget, QTableWidgetItem, QHeaderView,
)

from translator import Translator

logger = logging.getLogger(__name__)

# 結果キューの要素型: (原文リスト, 翻訳リスト)
_Frame = tuple[list[str], list[str]]


def _join_tokens(tokens: list[str]) -> str:
    """トークンリストを結合する。先頭が句読点のトークンは前にスペースを入れない。"""
    if not tokens:
        return ""
    out = tokens[0]
    for t in tokens[1:]:
        if t and t[0] in ".,!?;:":
            out += t
        else:
            out += " " + t
    return out


def _group_by_proximity(entries: list[tuple[str, int, int, int]]) -> list[str]:
    """
    GPS座標を使って (text, justify, x, y) リストをグループ化する。

    Step 1 — 同一行(y)内でのx方向クラスタリング:
        隣接トークン間のタイルギャップが MAX_INTRA_ROW_GAP(=1) を超えたら分割。
        '.' ',' も rows に含め、ギャップ計算に参加させることで
        "Meeting Area. Later" のような句読点をまたいだ結合を正しく行う。

    Step 2 — 隣接行(y_gap=1)の結合:
        以下の条件が揃ったとき前の行と結合（段落の折り返し）:
        ・y_gap == 1
        ・左余白(x_start)の差が MAX_X_MARGIN_DIFF 以内
        ・pending が文末（.!?）で終わっていない
        ・次行の先頭アルファベットが小文字
        サイドバー検出: y_gap==0 かつ x が pending_x から大きく離れている場合は
        pending を変えずに即時出力し、チュートリアル本文の結合を維持する。
    """
    MAX_INTRA_ROW_GAP = 1   # 単語間スペース=1タイルに合わせる（これを超えたら別要素）
    MAX_X_MARGIN_DIFF = 4   # 隣接行の左余白の許容差（タイル数）
    SIDEBAR_X_DIFF    = 8   # 同一行でこれ以上離れていればサイドバーとみなす

    def is_kept(text: str) -> bool:
        """アルファベット or 重要記号を含む場合のみ rows に追加する。"""
        return any(c.isalpha() for c in text) or any(c in ".,!?->" for c in text)

    # y行 → [(x, text)]（アルファベットまたは重要句読点を含むもの）
    rows: dict[int, list[tuple[int, str]]] = {}
    for text, _justify, x, y in entries:
        if is_kept(text):
            rows.setdefault(y, []).append((x, text))

    # Step 1: 各行をx順にソートし、大きなギャップでクラスター分割
    segments: list[tuple[int, int, str]] = []  # (y, x_start, text)
    for y in sorted(rows.keys()):
        tokens = sorted(rows[y])  # x昇順
        cluster: list[str] = []
        x_start = tokens[0][0]
        prev_x, prev_len = tokens[0][0], 0

        for x, text in tokens:
            gap = x - prev_x - prev_len
            if prev_len > 0 and gap > MAX_INTRA_ROW_GAP:
                joined = _join_tokens(cluster)
                if any(c.isalpha() for c in joined):
                    segments.append((y, x_start, joined))
                cluster = []
                x_start = x
            cluster.append(text)
            prev_x, prev_len = x, len(text)

        if cluster:
            joined = _join_tokens(cluster)
            if any(c.isalpha() for c in joined):
                segments.append((y, x_start, joined))

    if not segments:
        return []

    # Step 2: 隣接行を条件付きで結合
    # pending: 現在積み上げ中のセグメント（確定前）
    # サイドバー（同一y・遠いx）は pending を変えずに即出力する
    result: list[str] = []
    pending_y, pending_x, pending_text = segments[0]

    for y, x, text in segments[1:]:
        y_gap = y - pending_y
        x_margin_diff = abs(x - pending_x)
        ends_sent = pending_text[-1] in ".!?" if pending_text else True
        ends_with_arrow = pending_text.endswith('->')
        pending_has_internal_punct = any(c in ",." for c in pending_text[:-1])
        first_alpha = next((c for c in text if c.isalpha()), None)
        is_continuation = (
            (first_alpha is not None and first_alpha.islower())
            or ends_with_arrow
            or (not ends_sent and pending_has_internal_punct)
        )

        if (y_gap == 1
                and x_margin_diff <= MAX_X_MARGIN_DIFF
                and not ends_sent
                and is_continuation):
            # 段落の折り返し → pending に結合
            pending_text = pending_text + " " + text
            pending_y = y
        elif y_gap == 0 and x_margin_diff > SIDEBAR_X_DIFF:
            # 同一行の遠いx（サイドバー要素） → pending を変えず即出力
            result.append(text)
        else:
            # 新しいセクション → pending を確定して切り替え
            result.append(pending_text)
            pending_y, pending_x, pending_text = y, x, text

    result.append(pending_text)
    return result


class TranslationWindow(QMainWindow):
    """
    DF の隣に置く翻訳オーバーレイウィンドウ。
    原文列・翻訳列の 2 列テーブルで表示し、原文列でソートできる。
    """

    def __init__(self) -> None:
        super().__init__()
        self._translator = Translator()
        self._raw_queue: queue.Queue[list[str]] = queue.Queue(maxsize=4)
        self._result_queue: queue.Queue[_Frame] = queue.Queue(maxsize=4)
        self._connected = False
        self._last_frame: _Frame | None = None
        self._sort_column = -1
        self._sort_order: Qt.SortOrder | None = None

        self._setup_ui()
        self._start_translation_worker()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_result_queue)
        self._poll_timer.start(200)

    # ------------------------------------------------------------------
    # UI セットアップ
    # ------------------------------------------------------------------
    def _setup_ui(self) -> None:
        self.setWindowTitle("DF 日本語翻訳")
        self.setMinimumSize(860, 600)
        self.resize(960, 700)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 4)
        layout.setSpacing(0)

        self._table = QTableWidget()
        self._table.setColumnCount(2)
        self._table.setHorizontalHeaderLabels(["原文", "翻訳"])

        # 両列ともドラッグで幅変更できる Interactive モード
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)  # 翻訳列は余白を埋めつつドラッグ調整も可
        self._table.setColumnWidth(0, 380)  # 原文列の初期幅
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(False)
        self._table.setWordWrap(True)
        header.setSortIndicatorShown(False)
        header.sectionClicked.connect(self._on_header_clicked)

        self._table.setStyleSheet("""
            QTableWidget {
                background: #1e1e1e;
                color: #d4d4d4;
                gridline-color: #3a3a3a;
                font-family: 'Meiryo UI', 'Yu Gothic UI', sans-serif;
                font-size: 13px;
            }
            QTableWidget::item:alternate { background: #252526; }
            QTableWidget::item:selected  { background: #094771; color: #ffffff; }
            QHeaderView::section {
                background: #2d2d2d;
                color: #cccccc;
                padding: 4px 8px;
                border: none;
                border-right: 1px solid #3a3a3a;
                border-bottom: 1px solid #3a3a3a;
                font-weight: bold;
            }
        """)

        layout.addWidget(self._table)
        self.setCentralWidget(central)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._update_status()

    # ------------------------------------------------------------------
    # PipeReader から呼ばれるコールバック（非 GUI スレッドから呼ばれる）
    # ------------------------------------------------------------------
    def on_frame(self, entries: list[tuple[str, int, int, int]]) -> None:
        grouped = _group_by_proximity(entries)
        if not grouped:
            return
        if not self._connected:
            self._connected = True
        try:
            self._raw_queue.put_nowait(grouped)
        except queue.Full:
            pass

    # ------------------------------------------------------------------
    # 翻訳ワーカー（別スレッド）
    # ------------------------------------------------------------------
    def _start_translation_worker(self) -> None:
        t = threading.Thread(target=self._translation_worker, daemon=True, name="translator")
        t.start()

    def _translation_worker(self) -> None:
        while True:
            originals = self._raw_queue.get()
            translated = self._translator.translate_batch(originals)
            try:
                self._result_queue.put_nowait((originals, translated))
            except queue.Full:
                pass

    # ------------------------------------------------------------------
    # GUI ポーリング（QTimer）
    # ------------------------------------------------------------------
    def _poll_result_queue(self) -> None:
        latest: _Frame | None = None
        while True:
            try:
                latest = self._result_queue.get_nowait()
            except queue.Empty:
                break

        if latest is not None:
            self._display(*latest)
            self._update_status()

    def _display(self, originals: list[str], translated: list[str]) -> None:
        self._last_frame = (originals, translated)
        self._render_rows(originals, translated)

    def _render_rows(self, originals: list[str], translated: list[str]) -> None:
        self._table.setRowCount(0)
        for orig, trans in zip(originals, translated):
            row = self._table.rowCount()
            self._table.insertRow(row)
            orig_item = QTableWidgetItem(orig)
            trans_item = QTableWidgetItem(trans)
            orig_item.setFlags(orig_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            trans_item.setFlags(trans_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 0, orig_item)
            self._table.setItem(row, 1, trans_item)
        if self._sort_column >= 0:
            self._table.sortByColumn(self._sort_column, self._sort_order)
        self._table.resizeRowsToContents()

    # ------------------------------------------------------------------
    # ヘッダークリックによるソート（位置順 → 昇順 → 降順 → 位置順）
    # ------------------------------------------------------------------
    def _on_header_clicked(self, col: int) -> None:
        header = self._table.horizontalHeader()
        if self._sort_column == col:
            if self._sort_order == Qt.SortOrder.AscendingOrder:
                self._sort_order = Qt.SortOrder.DescendingOrder
            else:
                # 降順 → 位置順に戻す
                self._sort_column = -1
                self._sort_order = None
                header.setSortIndicatorShown(False)
                if self._last_frame:
                    self._render_rows(*self._last_frame)
                return
        else:
            self._sort_column = col
            self._sort_order = Qt.SortOrder.AscendingOrder

        self._table.sortByColumn(self._sort_column, self._sort_order)
        header.setSortIndicatorShown(True)
        header.setSortIndicator(self._sort_column, self._sort_order)

    def _update_status(self) -> None:
        if self._connected:
            self._status_bar.showMessage(f"接続中 ・ {self._translator.engine_name}")
        else:
            self._status_bar.showMessage("DFの起動を待っています…")
