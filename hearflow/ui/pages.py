"""Seven functional pages used by the HearFlow Studio main window."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QItemSelectionModel, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileSystemModel,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableView,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from hearflow.ui.table_models import JobTableModel, SegmentTableModel

_STAGES = (
    (1, "媒體與環境檢查"),
    (2, "Qwen3-ASR 語音辨識"),
    (3, "文字清理"),
    (4, "字幕翻譯"),
    (5, "字幕 QA"),
    (6, "字幕與文字匯出"),
    (7, "報告與完成檢查"),
)
_STAGE_ALIASES = {
    "inspect": 0,
    "asr": 1,
    "clean": 2,
    "translate": 3,
    "qa": 4,
    "export": 5,
    "complete": 6,
}


def _configure_table(table: QTableView) -> None:
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setWordWrap(False)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setStretchLastSection(True)


class WorkflowPage(QWidget):
    """Three-column overview matching the reference application's structure."""

    environment_requested = Signal()
    start_requested = Signal()
    cancel_requested = Signal()
    add_media_requested = Signal()
    job_selected = Signal(str)

    def __init__(self, job_model: JobTableModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._job_model = job_model
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("尚未開始")

        self.stage_list = QListWidget()
        self.stage_list.setSpacing(4)
        for number, title in _STAGES:
            item = QListWidgetItem(f"○  {number}. {title}　尚未執行")
            item.setData(Qt.ItemDataRole.UserRole, number)
            self.stage_list.addItem(item)

        left = QGroupBox("工作進度")
        left.setProperty("zone", "asr")
        left.setMinimumWidth(220)
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(self.progress)
        left_layout.addWidget(self.stage_list, 1)

        self.scope = QLineEdit()
        self.scope.setPlaceholderText("目前選取的媒體")
        self.scope.setReadOnly(True)
        self.translate = QCheckBox("辨識後翻譯為繁體中文")
        self.force_retry = QCheckBox("重新執行已完成項目")
        self.status_hint = QLabel("先加入媒體，再選擇一列開始處理。")
        self.status_hint.setWordWrap(True)
        self.status_hint.setProperty("muted", True)
        scan = QPushButton("環境檢查")
        scan.clicked.connect(self.environment_requested)
        add = QPushButton("加入媒體")
        add.clicked.connect(self.add_media_requested)
        start = QPushButton("開始完整流程")
        start.setProperty("primary", True)
        start.clicked.connect(self.start_requested)
        cancel = QPushButton("取消目前工作")
        cancel.clicked.connect(self.cancel_requested)
        self.start_button = start
        self.cancel_button = cancel
        self.cancel_button.setEnabled(False)
        action_grid = QGridLayout()
        action_grid.addWidget(scan, 0, 0)
        action_grid.addWidget(add, 0, 1)
        action_grid.addWidget(start, 1, 0, 1, 2)
        action_grid.addWidget(cancel, 2, 0, 1, 2)

        controls = QGroupBox("執行控制")
        controls.setMinimumWidth(300)
        controls_layout = QVBoxLayout(controls)
        controls_layout.addWidget(QLabel("目前項目"))
        controls_layout.addWidget(self.scope)
        controls_layout.addWidget(self.translate)
        controls_layout.addWidget(self.force_retry)
        controls_layout.addLayout(action_grid)
        controls_layout.addStretch(1)
        controls_layout.addWidget(self.status_hint)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("處理紀錄會顯示在這裡。")
        log_group = QGroupBox("工作日誌")
        log_group.setMinimumWidth(260)
        log_layout = QVBoxLayout(log_group)
        log_layout.addWidget(self.log)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(left)
        self.splitter.addWidget(controls)
        self.splitter.addWidget(log_group)
        self.splitter.setSizes([270, 430, 350])
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setStretchFactor(2, 3)
        self.splitter.setChildrenCollapsible(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.splitter)

    def set_current_job(self, name: str) -> None:
        self.scope.setText(name)

    def set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)

    def show_engine_ready(self) -> None:
        message = "辨識引擎已就緒，等待加入媒體。"
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat(message)
        for index, (number, title) in enumerate(_STAGES):
            item = self.stage_list.item(index)
            if index == 0:
                item.setText(f"✓  {number}. {title}　引擎就緒")
            else:
                item.setText(f"○  {number}. {title}　尚未執行")
        self.status_hint.setText(message)
        self.append_log(message)

    def set_progress(self, stage: str, percent: int, message: str) -> None:
        self.progress.setValue(percent)
        self.progress.setFormat(f"{percent}%　{message}")
        current = _STAGE_ALIASES.get(stage)
        if current is not None:
            for index in range(self.stage_list.count()):
                item = self.stage_list.item(index)
                number, title = _STAGES[index]
                if index < current or stage == "complete":
                    prefix, suffix = "✓", "已完成"
                elif index == current:
                    prefix, suffix = "→", "執行中"
                else:
                    prefix, suffix = "○", "尚未執行"
                item.setText(f"{prefix}  {number}. {title}　{suffix}")
        self.status_hint.setText(message)
        self.append_log(message)

    def append_log(self, message: str) -> None:
        self.log.appendPlainText(message)

    def load_events(self, events: list[dict[str, Any]]) -> None:
        self.log.clear()
        for event in events:
            when = str(event.get("created_at", "")).replace("T", " ")[:19]
            self.log.appendPlainText(f"[{when}] {event.get('message', '')}")


class QueuePage(QWidget):
    """Media queue with actionable selection."""

    add_requested = Signal()
    start_requested = Signal()
    open_source_requested = Signal()
    selected = Signal(str)

    def __init__(self, model: JobTableModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.table = QTableView()
        self.table.setModel(model)
        _configure_table(self.table)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(lambda _index: self.open_source_requested.emit())
        add = QPushButton("加入媒體…")
        add.clicked.connect(self.add_requested)
        start = QPushButton("處理選取項目")
        start.setProperty("primary", True)
        start.clicked.connect(self.start_requested)
        open_source = QPushButton("開啟來源位置")
        open_source.clicked.connect(self.open_source_requested)
        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(start)
        buttons.addWidget(open_source)
        buttons.addStretch(1)
        note = QLabel("支援 WAV、MP3、M4A、MP4、MOV、MKV、WebM、FLAC 等常用格式。")
        note.setProperty("muted", True)
        layout = QVBoxLayout(self)
        layout.addLayout(buttons)
        layout.addWidget(note)
        layout.addWidget(self.table, 1)

    def select_job(self, job_id: str) -> None:
        model = self.table.model()
        for row in range(model.rowCount()):
            if model.index(row, 0).data(Qt.ItemDataRole.UserRole) == job_id:
                selection = self.table.selectionModel()
                selection.select(
                    model.index(row, 0),
                    QItemSelectionModel.SelectionFlag.ClearAndSelect
                    | QItemSelectionModel.SelectionFlag.Rows,
                )
                return

    def _selection_changed(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            job_id = rows[0].data(Qt.ItemDataRole.UserRole)
            if job_id:
                self.selected.emit(str(job_id))


class EditorPage(QWidget):
    """Searchable, editable subtitle text grid."""

    save_requested = Signal()
    split_requested = Signal(int)
    merge_requested = Signal(int)

    def __init__(self, model: SegmentTableModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = model
        self.table = QTableView()
        self.table.setModel(model)
        _configure_table(self.table)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜尋原文或翻譯…")
        self.search.textChanged.connect(self._search)
        save = QPushButton("儲存編輯")
        save.setProperty("primary", True)
        save.clicked.connect(self.save_requested)
        split = QPushButton("在游標位置分段")
        split.clicked.connect(self._split)
        merge = QPushButton("與下一段合併")
        merge.clicked.connect(self._merge)
        buttons = QHBoxLayout()
        buttons.addWidget(self.search, 1)
        buttons.addWidget(split)
        buttons.addWidget(merge)
        buttons.addWidget(save)
        layout = QVBoxLayout(self)
        layout.addLayout(buttons)
        layout.addWidget(self.table, 1)

    def _current_row(self) -> int:
        return self.table.currentIndex().row()

    def _split(self) -> None:
        row = self._current_row()
        if row >= 0:
            self.split_requested.emit(row)

    def _merge(self) -> None:
        row = self._current_row()
        if row >= 0:
            self.merge_requested.emit(row)

    def _search(self, text: str) -> None:
        needle = text.casefold()
        for row in range(self.model.rowCount()):
            item = self.model.segment_at(row)
            visible = (
                not needle
                or needle in (item.source_text + " " + (item.translated_text or "")).casefold()
            )
            self.table.setRowHidden(row, not visible)


class TimelinePage(QWidget):
    """Timestamp review with an explicit upstream accuracy disclosure."""

    save_requested = Signal()

    def __init__(self, model: SegmentTableModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        notice = QFrame()
        notice.setProperty("warningPanel", True)
        icon = QLabel("⚠")
        icon.setProperty("warningIcon", True)
        text = QLabel(
            "<b>目前時間精度：chunk（片段級）</b><br>"
            "Qwen3-ASR Gateway 不提供逐字對齊。這裡可人工修正每段開始與結束時間，"
            "但畫面不會把片段級時間誤稱為逐字精準。"
        )
        text.setWordWrap(True)
        notice_layout = QHBoxLayout(notice)
        notice_layout.addWidget(icon)
        notice_layout.addWidget(text, 1)
        table = QTableView()
        table.setModel(model)
        _configure_table(table)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        save = QPushButton("儲存時間修正")
        save.setProperty("primary", True)
        save.clicked.connect(self.save_requested)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(save)
        layout = QVBoxLayout(self)
        layout.addWidget(notice)
        layout.addWidget(table, 1)
        layout.addLayout(row)


class TranslationPage(QWidget):
    """Project vocabulary and translation controls."""

    project_settings_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.enabled = QCheckBox("啟用辨識後翻譯")
        self.provider = QComboBox()
        self.provider.addItems(["Ollama / OpenAI 相容服務", "LM Studio", "OpenAI 相容雲端服務"])
        self.model = QLineEdit()
        self.model.setPlaceholderText("例如 qwen3:8b")
        self.target = QLineEdit("繁體中文（台灣）")
        self.style = QComboBox()
        self.style.addItems(["自然口語", "精簡字幕", "正式書面", "保留角色語氣"])
        form = QFormLayout()
        form.addRow("", self.enabled)
        form.addRow("翻譯來源", self.provider)
        form.addRow("模型", self.model)
        form.addRow("目標語言", self.target)
        form.addRow("翻譯風格", self.style)
        provider_group = QGroupBox("翻譯設定")
        provider_group.setProperty("zone", "translation")
        provider_group.setLayout(form)

        self.glossary = QPlainTextEdit()
        self.glossary.setPlaceholderText(
            "每行一組詞彙，例如：\nQwen3-ASR = Qwen3-ASR\nchunk = 片段"
        )
        glossary_group = QGroupBox("專案詞彙表")
        glossary_group.setProperty("zone", "project")
        glossary_layout = QVBoxLayout(glossary_group)
        glossary_layout.addWidget(QLabel("詞彙表會作為翻譯提示；不會直接覆寫辨識原文。"))
        glossary_layout.addWidget(self.glossary)
        save = QPushButton("套用翻譯設定")
        save.setProperty("primary", True)
        save.clicked.connect(self.project_settings_requested)
        layout = QVBoxLayout(self)
        layout.addWidget(provider_group)
        layout.addWidget(glossary_group, 1)
        layout.addWidget(save, alignment=Qt.AlignmentFlag.AlignRight)


class FilesPage(QWidget):
    """Browse project outputs without leaving the application."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = QFileSystemModel(self)
        self.model.setReadOnly(True)
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setAlternatingRowColors(True)
        self.tree.doubleClicked.connect(self._open)
        for column in range(1, 4):
            self.tree.hideColumn(column)
        self.root_label = QLabel("尚未開啟專案")
        self.root_label.setProperty("muted", True)
        refresh = QPushButton("重新整理")
        refresh.clicked.connect(lambda: self.set_root(self.root_label.property("path") or ""))
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(self.root_label, 1)
        row.addWidget(refresh)
        layout.addLayout(row)
        layout.addWidget(self.tree, 1)

    def set_root(self, path: str | Path) -> None:
        root = str(path)
        if not root:
            return
        self.root_label.setText(root)
        self.root_label.setProperty("path", root)
        index = self.model.setRootPath(root)
        self.tree.setRootIndex(index)

    def _open(self, index: Any) -> None:
        path = self.model.filePath(index)
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))


class ReportsPage(QWidget):
    """Export shortcuts and generated report list."""

    export_requested = Signal(str)
    burn_requested = Signal()
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        title = QLabel("產生可交付檔案")
        title.setProperty("sectionTitle", True)
        subtitle = QLabel("輸出一律建立新檔，不會覆寫原始影音。已有同名檔案時會自動保留版本。")
        subtitle.setWordWrap(True)
        subtitle.setProperty("muted", True)
        formats = QHBoxLayout()
        for label, value in (
            ("SRT 字幕", "srt"),
            ("VTT 字幕", "vtt"),
            ("ASS 樣式字幕", "ass"),
            ("純文字", "txt"),
            ("JSON", "json"),
        ):
            button = QPushButton(label)
            button.clicked.connect(
                lambda _checked=False, selected=value: self.export_requested.emit(selected)
            )
            formats.addWidget(button)
        formats.addStretch(1)
        self.burn_button = QPushButton("將 ASS 壓入影片（另存新檔）")
        self.burn_button.setProperty("primary", True)
        self.burn_button.setEnabled(False)
        self.burn_button.clicked.connect(self.burn_requested)
        self.report_list = QListWidget()
        refresh = QPushButton("重新整理檔案清單")
        refresh.clicked.connect(self.refresh_requested)
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addLayout(formats)
        layout.addWidget(self.burn_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(QLabel("報告與輸出"))
        layout.addWidget(self.report_list, 1)
        layout.addWidget(refresh, alignment=Qt.AlignmentFlag.AlignRight)

    def set_files(self, files: list[Path]) -> None:
        self.report_list.clear()
        for path in files:
            item = QListWidgetItem(path.name)
            item.setToolTip(str(path))
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.report_list.addItem(item)

    def set_burn_enabled(self, enabled: bool, reason: str = "") -> None:
        self.burn_button.setEnabled(enabled)
        self.burn_button.setToolTip(reason)
