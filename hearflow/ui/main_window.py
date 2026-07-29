"""Main Windows desktop shell for HearFlow Studio."""

from __future__ import annotations

import os
import subprocess
import traceback
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, TypeVar

from PySide6.QtCore import (
    QObject,
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from hearflow.application.workflow import StudioWorkflow
from hearflow.domain.models import ArtifactKind, ExportOptions, JobStatus
from hearflow.services.engine import EngineManager, application_root
from hearflow.services.service_factory import (
    build_speech_synthesizer,
    build_transcription_client,
    build_translator,
)
from hearflow.services.settings import AppSettings, SecretStore, SettingsRepository
from hearflow.services.speech import SpeechRenderService
from hearflow.services.translation_engine import TranslationEngineManager
from hearflow.ui.dialogs import EnvironmentReportDialog, NewProjectDialog
from hearflow.ui.settings_v2 import SettingsDialog
from hearflow.ui.pages import (
    EditorPage,
    FilesPage,
    QueuePage,
    ReportsPage,
    TimelinePage,
    TranslationPage,
    WorkflowPage,
)
from hearflow.ui.table_models import JobTableModel, SegmentTableModel

_T = TypeVar("_T")


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str, str)
    progress = Signal(str, int, str)
    finished = Signal()


class BackgroundTask(QRunnable):
    """Execute one callable outside the Qt GUI thread."""

    def __init__(
        self,
        operation: Callable[[Callable[[str, int, str], None]], Any],
    ) -> None:
        super().__init__()
        self.operation = operation
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.operation(self.signals.progress.emit)
        except BaseException as exc:  # forwarded to main-thread presentation
            with suppress(RuntimeError):
                self.signals.error.emit(str(exc), traceback.format_exc())
        else:
            with suppress(RuntimeError):
                self.signals.result.emit(result)
        finally:
            with suppress(RuntimeError):
                self.signals.finished.emit()


class ProjectSettingsPanel(QGroupBox):
    """Compact project metadata panel located above the workflow tabs."""

    save_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("專案設定", parent)
        self.name = QLineEdit()
        self.source_language = QComboBox()
        for label, value in (
            ("自動偵測", "auto"),
            ("中文", "zh"),
            ("英文", "en"),
            ("日文", "ja"),
            ("韓文", "ko"),
            ("粵語", "yue"),
        ):
            self.source_language.addItem(label, value)
        self.target_language = QComboBox()
        for label, value in (
            ("繁體中文（台灣）", "zh-TW"),
            ("英文", "en"),
            ("日文", "ja"),
            ("不翻譯", ""),
        ):
            self.target_language.addItem(label, value)
        self.style = QComboBox()
        self.style.addItems(["自然口語", "精簡字幕", "正式書面", "保留角色語氣"])
        save = QPushButton("儲存專案設定")
        save.clicked.connect(self.save_requested)
        layout = QGridLayout(self)
        layout.addWidget(QLabel("專案名稱"), 0, 0)
        layout.addWidget(self.name, 0, 1, 1, 3)
        layout.addWidget(QLabel("來源語言"), 1, 0)
        layout.addWidget(self.source_language, 1, 1)
        layout.addWidget(QLabel("目標語言"), 1, 2)
        layout.addWidget(self.target_language, 1, 3)
        layout.addWidget(QLabel("翻譯風格"), 2, 0)
        layout.addWidget(self.style, 2, 1, 1, 2)
        layout.addWidget(save, 2, 3)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)

    def set_enabled_state(self, enabled: bool) -> None:
        self.setEnabled(enabled)


class MainWindow(QMainWindow):
    """Non-technical, end-to-end desktop presentation."""

    def __init__(
        self,
        workflow: StudioWorkflow,
        settings: AppSettings | None = None,
        settings_repository: SettingsRepository | None = None,
        engine_manager: EngineManager | None = None,
        secret_store: SecretStore | None = None,
        translation_engine_manager: TranslationEngineManager | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.workflow = workflow
        self.settings = settings or AppSettings()
        self.settings_repository = settings_repository
        self.engine_manager = engine_manager or EngineManager(self.settings.engine)
        self.secret_store = secret_store or SecretStore()
        self.translation_engine_manager = translation_engine_manager or TranslationEngineManager(
            self.settings.translation_engine,
            runtime_root=self.engine_manager.runtime_root,
        )
        self._selected_job_id: str | None = None
        self._running_job_id: str | None = None
        self._running_operation: str | None = None
        self._engine_operation_active = False
        self._translation_engine_active = False
        self._active_tasks: set[BackgroundTask] = set()
        self._thread_pool = QThreadPool.globalInstance()
        self.setWindowTitle("聽序 HearFlow Studio v0.2.0")
        self.resize(1520, 920)
        self.setMinimumSize(1180, 700)
        self._build_menu()
        self._build_ui()
        self._connect_actions()
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("就緒")
        QTimer.singleShot(350, self.check_environment)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("檔案")
        self.new_action = QAction("建立新專案…", self)
        self.new_action.setShortcut("Ctrl+N")
        self.open_action = QAction("開啟專案…", self)
        self.open_action.setShortcut("Ctrl+O")
        self.add_action = QAction("加入媒體…", self)
        self.add_action.setShortcut("Ctrl+I")
        self.open_folder_action = QAction("開啟專案資料夾", self)
        exit_action = QAction("結束", self)
        exit_action.triggered.connect(self.close)
        file_menu.addActions(
            [self.new_action, self.open_action, self.add_action, self.open_folder_action]
        )
        file_menu.addSeparator()
        file_menu.addAction(exit_action)

        tools_menu = self.menuBar().addMenu("工具")
        self.environment_action = QAction("環境檢查", self)
        self.install_engine_action = QAction("快速安裝本機引擎…", self)
        self.start_engine_action = QAction("啟動本機引擎", self)
        self.stop_engine_action = QAction("停止本機引擎", self)
        self.settings_action = QAction("偏好設定…", self)
        tools_menu.addActions(
            [
                self.environment_action,
                self.install_engine_action,
                self.start_engine_action,
                self.stop_engine_action,
                self.settings_action,
            ]
        )

        help_menu = self.menuBar().addMenu("說明")
        about = QAction("關於聽序", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    def _build_ui(self) -> None:
        shell = QWidget()
        outer = QVBoxLayout(shell)
        outer.setContentsMargins(12, 10, 12, 12)
        outer.setSpacing(10)

        header = QHBoxLayout()
        brand = QLabel("聽序")
        brand.setObjectName("brand")
        product = QLabel("HearFlow Studio")
        product.setObjectName("productName")
        tagline = QLabel("Qwen3-ASR 轉錄・字幕校訂・翻譯與交付")
        tagline.setProperty("muted", True)
        header.addWidget(brand)
        header.addWidget(product)
        header.addSpacing(8)
        header.addWidget(tagline)
        header.addStretch(1)
        self.header_status = QLabel("● 正在檢查引擎")
        self.header_status.setObjectName("headerStatus")
        header.addWidget(self.header_status)
        outer.addLayout(header)

        engine_group = QGroupBox("辨識引擎")
        engine_group.setProperty("zone", "asr")
        engine_layout = QGridLayout(engine_group)
        self.engine_state = QLabel("檢查中")
        self.engine_state.setObjectName("metricValue")
        self.engine_detail = QLabel("正在連線 Qwen3-ASR Gateway…")
        self.engine_detail.setProperty("muted", True)
        self.model_value = QLabel(self.settings.engine.model_id)
        self.model_value.setObjectName("metricValue")
        self.backend_value = QLabel(self.settings.engine.backend.upper())
        self.backend_value.setObjectName("metricValue")
        self.queue_value = QLabel("0")
        self.queue_value.setObjectName("metricValue")
        self.engine_progress = QProgressBar()
        self.engine_progress.setRange(0, 100)
        self.engine_progress.setValue(0)
        self.engine_progress.setTextVisible(False)
        check_button = QPushButton("重新檢查")
        check_button.clicked.connect(self.check_environment)
        self.install_engine_button = QPushButton("快速安裝")
        self.install_engine_button.clicked.connect(self.quick_install_engine)
        self.start_engine_button = QPushButton("啟動引擎")
        self.start_engine_button.setProperty("success", True)
        self.start_engine_button.clicked.connect(self.start_local_engine)
        self.stop_engine_button = QPushButton("停止")
        self.stop_engine_button.setProperty("danger", True)
        self.stop_engine_button.clicked.connect(self.stop_local_engine)
        engine_layout.addWidget(QLabel("狀態"), 0, 0)
        engine_layout.addWidget(self.engine_state, 1, 0)
        engine_layout.addWidget(self.engine_detail, 2, 0)
        engine_layout.addWidget(self.engine_progress, 3, 0)
        engine_layout.addWidget(QLabel("模型"), 0, 1)
        engine_layout.addWidget(self.model_value, 1, 1, 2, 1)
        engine_layout.addWidget(QLabel("後端"), 0, 2)
        engine_layout.addWidget(self.backend_value, 1, 2, 2, 1)
        engine_layout.addWidget(QLabel("佇列"), 0, 3)
        engine_layout.addWidget(self.queue_value, 1, 3, 2, 1)
        button_row = QHBoxLayout()
        button_row.addWidget(self.install_engine_button)
        button_row.addWidget(self.start_engine_button)
        button_row.addWidget(self.stop_engine_button)
        button_row.addWidget(check_button)
        engine_layout.addLayout(button_row, 4, 0, 1, 4)
        engine_layout.setColumnStretch(0, 3)
        engine_layout.setColumnStretch(1, 2)
        engine_layout.setColumnStretch(2, 1)
        engine_layout.setColumnStretch(3, 1)
        outer.addWidget(engine_group)

        translation_engine_group = QGroupBox("翻譯引擎（TranslateGemma）")
        translation_engine_group.setProperty("zone", "translation")
        te_layout = QGridLayout(translation_engine_group)
        self.te_state = QLabel("未啟用" if not self.settings.translation_engine.enabled else "檢查中")
        self.te_state.setObjectName("metricValue")
        self.te_detail = QLabel(
            "在偏好設定啟用翻譯引擎後，可按「啟動」載入 TranslateGemma 4B。"
            if not self.settings.translation_engine.enabled
            else "正在檢查翻譯引擎狀態…"
        )
        self.te_detail.setProperty("muted", True)
        self.te_model_value = QLabel(self.settings.translation_engine.model_id)
        self.te_model_value.setObjectName("metricValue")
        self.te_backend_value = QLabel(self.settings.translation_engine.backend.upper())
        self.te_backend_value.setObjectName("metricValue")
        self.te_port_value = QLabel(str(self.settings.translation_engine.port))
        self.te_port_value.setObjectName("metricValue")
        self.te_progress = QProgressBar()
        self.te_progress.setRange(0, 100)
        self.te_progress.setValue(0)
        self.te_progress.setTextVisible(False)
        self.start_te_button = QPushButton("啟動翻譯引擎")
        self.start_te_button.setProperty("success", True)
        self.start_te_button.clicked.connect(self.start_translation_engine)
        self.stop_te_button = QPushButton("停止")
        self.stop_te_button.setProperty("danger", True)
        self.stop_te_button.clicked.connect(self.stop_translation_engine)
        te_check = QPushButton("重新檢查")
        te_check.clicked.connect(self.check_translation_engine)
        te_layout.addWidget(QLabel("狀態"), 0, 0)
        te_layout.addWidget(self.te_state, 1, 0)
        te_layout.addWidget(self.te_detail, 2, 0)
        te_layout.addWidget(self.te_progress, 3, 0)
        te_layout.addWidget(QLabel("模型"), 0, 1)
        te_layout.addWidget(self.te_model_value, 1, 1, 2, 1)
        te_layout.addWidget(QLabel("後端"), 0, 2)
        te_layout.addWidget(self.te_backend_value, 1, 2, 2, 1)
        te_layout.addWidget(QLabel("埠"), 0, 3)
        te_layout.addWidget(self.te_port_value, 1, 3, 2, 1)
        te_button_row = QHBoxLayout()
        te_button_row.addWidget(self.start_te_button)
        te_button_row.addWidget(self.stop_te_button)
        te_button_row.addWidget(te_check)
        te_layout.addLayout(te_button_row, 4, 0, 1, 4)
        te_layout.setColumnStretch(0, 3)
        te_layout.setColumnStretch(1, 2)
        te_layout.setColumnStretch(2, 1)
        te_layout.setColumnStretch(3, 1)
        outer.addWidget(translation_engine_group)

        project_row = QHBoxLayout()
        project_row.addWidget(QLabel("專案資料夾"))
        self.project_path = QLineEdit()
        self.project_path.setReadOnly(True)
        self.project_path.setPlaceholderText("尚未建立或開啟專案")
        project_row.addWidget(self.project_path, 1)
        browse = QPushButton("開啟…")
        browse.clicked.connect(self.open_project)
        new = QPushButton("建立新專案")
        new.setProperty("primary", True)
        new.clicked.connect(self.new_project)
        project_row.addWidget(browse)
        project_row.addWidget(new)
        outer.addLayout(project_row)

        self.project_settings = ProjectSettingsPanel()
        self.project_settings.set_enabled_state(False)
        outer.addWidget(self.project_settings)

        self.job_model = JobTableModel(self)
        self.segment_model = SegmentTableModel(self)
        self.workflow_page = WorkflowPage(self.job_model)
        self.queue_page = QueuePage(self.job_model)
        self.editor_page = EditorPage(self.segment_model)
        self.timeline_page = TimelinePage(self.segment_model)
        self.translation_page = TranslationPage()
        self.files_page = FilesPage()
        self.reports_page = ReportsPage()
        self.tabs = QTabWidget()
        for title, page in (
            ("工作流程", self.workflow_page),
            ("轉錄佇列", self.queue_page),
            ("文字校訂", self.editor_page),
            ("字幕時間軸", self.timeline_page),
            ("詞彙與翻譯", self.translation_page),
            ("檔案狀態", self.files_page),
            ("報告與匯出", self.reports_page),
        ):
            self.tabs.addTab(page, title)
        outer.addWidget(self.tabs, 1)

        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.content_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.content_scroll.setWidget(shell)
        self.setCentralWidget(self.content_scroll)

    def _connect_actions(self) -> None:
        self.new_action.triggered.connect(self.new_project)
        self.open_action.triggered.connect(self.open_project)
        self.add_action.triggered.connect(self.add_media)
        self.open_folder_action.triggered.connect(self.open_project_folder)
        self.environment_action.triggered.connect(self.check_environment)
        self.install_engine_action.triggered.connect(self.quick_install_engine)
        self.start_engine_action.triggered.connect(self.start_local_engine)
        self.stop_engine_action.triggered.connect(self.stop_local_engine)
        self.settings_action.triggered.connect(self.show_settings)
        self.workflow_page.environment_requested.connect(self.check_environment)
        self.workflow_page.add_media_requested.connect(self.add_media)
        self.workflow_page.start_requested.connect(self.start_selected_job)
        self.workflow_page.cancel_requested.connect(self.cancel_current_job)
        self.queue_page.add_requested.connect(self.add_media)
        self.queue_page.start_requested.connect(self.start_selected_job)
        self.queue_page.open_source_requested.connect(self.open_selected_source)
        self.queue_page.selected.connect(self.select_job)
        self.editor_page.save_requested.connect(self.save_segments)
        self.editor_page.split_requested.connect(self.split_segment)
        self.editor_page.merge_requested.connect(self.merge_segment)
        self.timeline_page.save_requested.connect(self.save_segments)
        self.project_settings.save_requested.connect(self.save_project_settings)
        self.translation_page.project_settings_requested.connect(self.apply_translation_page)
        self.reports_page.export_requested.connect(self.export_selected_format)
        self.reports_page.burn_requested.connect(self.burn_subtitles)
        self.reports_page.speech_requested.connect(self.synthesize_speech)
        self.reports_page.refresh_requested.connect(self.refresh_files)
        self.segment_model.validation_error.connect(
            lambda message: self.statusBar().showMessage(message, 6_000)
        )

    def _run_background(
        self,
        operation: Callable[[Callable[[str, int, str], None]], _T],
        *,
        on_result: Callable[[_T], None] | None = None,
        busy_message: str = "處理中…",
        show_errors: bool = True,
    ) -> BackgroundTask:
        task = BackgroundTask(operation)
        self._active_tasks.add(task)
        self.statusBar().showMessage(busy_message)
        task.signals.progress.connect(self.workflow_page.set_progress)
        if on_result is not None:
            task.signals.result.connect(on_result)
        task.signals.error.connect(
            lambda message, detail: (
                self._show_task_error(message, detail)
                if show_errors
                else self._quiet_error(message)
            )
        )
        task.signals.finished.connect(lambda: self._task_finished(task))
        self._thread_pool.start(task)
        return task

    def _task_finished(self, task: BackgroundTask) -> None:
        self._active_tasks.discard(task)
        if not self._active_tasks:
            self.statusBar().showMessage("就緒")

    def _show_task_error(self, message: str, detail: str) -> None:
        self.workflow_page.append_log(f"錯誤：{message}")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("無法完成")
        box.setText(message or "發生未預期的錯誤。")
        box.setDetailedText(detail)
        box.exec()
        self._finish_job_ui()
        self.refresh_project()

    def _quiet_error(self, message: str) -> None:
        self.engine_state.setText("未連線")
        self.engine_detail.setText(message)
        self.header_status.setText("● 引擎尚未就緒")
        self.header_status.setProperty("state", "offline")
        self.header_status.style().unpolish(self.header_status)
        self.header_status.style().polish(self.header_status)
        self.engine_progress.setValue(0)

    @Slot()
    def new_project(self) -> None:
        start = self.settings.last_project_dir or str(Path.home() / "Documents")
        dialog = NewProjectDialog(start, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        parent, name = dialog.values
        target = parent / name
        self._run_background(
            lambda _progress: self.workflow.create_project(target, name),
            on_result=lambda _repo: self._project_opened(),
            busy_message="正在建立專案…",
        )

    @Slot()
    def open_project(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "選擇 HearFlow 專案資料夾",
            self.settings.last_project_dir,
        )
        if not folder:
            return
        self._run_background(
            lambda _progress: self.workflow.open_project(folder),
            on_result=lambda _repo: self._project_opened(),
            busy_message="正在開啟專案…",
        )

    def _project_opened(self) -> None:
        self._selected_job_id = None
        self.segment_model.set_segments([])
        self.refresh_project()
        repository = self.workflow.repository
        if repository is not None:
            self.files_page.set_root(repository.root)
        self.statusBar().showMessage("專案已開啟", 4_000)

    @Slot()
    def add_media(self) -> None:
        if self.workflow.repository is None:
            QMessageBox.information(self, "請先建立專案", "加入媒體前，請先建立或開啟專案。")
            return
        paths, _selected_filter = QFileDialog.getOpenFileNames(
            self,
            "加入影音檔案",
            "",
            (
                "影音檔案 (*.wav *.mp3 *.m4a *.mp4 *.mpeg *.mpga *.webm "
                "*.ogg *.opus *.flac *.aac *.mov *.mkv);;所有檔案 (*.*)"
            ),
        )
        if not paths:
            return
        self._run_background(
            lambda _progress: self.workflow.add_media(paths),
            on_result=self._media_added,
            busy_message=f"正在加入 {len(paths)} 個檔案…",
        )

    def _media_added(self, jobs: list[Any]) -> None:
        self.refresh_project()
        if jobs:
            self.select_job(jobs[-1].id)
            self.queue_page.select_job(jobs[-1].id)
        self.tabs.setCurrentWidget(self.queue_page)

    def refresh_project(self) -> None:
        repository = self.workflow.repository
        enabled = repository is not None
        self.add_action.setEnabled(enabled)
        self.open_folder_action.setEnabled(enabled)
        self.project_settings.set_enabled_state(enabled)
        if repository is None:
            self.project_path.clear()
            self.job_model.set_jobs([])
            self.queue_value.setText("0")
            return
        manifest = repository.manifest()
        self.project_path.setText(str(repository.root))
        self.project_settings.name.setText(manifest.name)
        self._select_combo_data(self.project_settings.source_language, manifest.source_language)
        self._select_combo_data(
            self.project_settings.target_language, manifest.target_language or ""
        )
        self.job_model.set_jobs(manifest.jobs)
        active = sum(
            job.status
            not in {
                JobStatus.COMPLETED,
                JobStatus.CANCELLED,
                JobStatus.FAILED,
            }
            for job in manifest.jobs
        )
        self.queue_value.setText(str(active))
        self.workflow_page.load_events(repository.list_events(limit=300))
        self.files_page.set_root(repository.root)
        self.refresh_files()
        if self._selected_job_id is not None:
            try:
                job = repository.get_job(self._selected_job_id)
            except KeyError:
                self._selected_job_id = None
            else:
                self.workflow_page.set_current_job(job.source_name)
                self.segment_model.set_segments(repository.load_segments(self._selected_job_id))
                video_ready = bool(job.metadata is not None and job.metadata.video_streams)
                self.reports_page.set_burn_enabled(
                    video_ready and self.segment_model.rowCount() > 0,
                    ("另存含字幕 MP4" if video_ready else "選取項目沒有影像串流，無法壓入字幕"),
                )

    @Slot(str)
    def select_job(self, job_id: str) -> None:
        repository = self.workflow.repository
        if repository is None:
            return
        try:
            job = repository.get_job(job_id)
        except KeyError:
            return
        self._selected_job_id = job_id
        self.workflow_page.set_current_job(job.source_name)
        self.segment_model.set_segments(repository.load_segments(job_id))
        video_ready = bool(job.metadata is not None and job.metadata.video_streams)
        self.reports_page.set_burn_enabled(
            video_ready and self.segment_model.rowCount() > 0,
            ("另存含字幕 MP4" if video_ready else "這是純音訊或尚未完成媒體檢查"),
        )

    @Slot()
    def start_selected_job(self) -> None:
        if self._engine_operation_active:
            QMessageBox.information(
                self,
                "辨識引擎準備中",
                "請等待目前的引擎驗證、安裝或啟停完成。",
            )
            return
        if self._selected_job_id is None:
            QMessageBox.information(self, "尚未選取", "請先在轉錄佇列選擇一個媒體檔案。")
            return
        if self._running_job_id is not None:
            QMessageBox.information(self, "工作執行中", "請等待目前工作完成或先取消。")
            return
        job_id = self._selected_job_id
        self._running_job_id = job_id
        self._running_operation = "pipeline"
        self._refresh_engine_controls()
        self.workflow_page.set_running(True)
        self.engine_progress.setRange(0, 100)
        self.engine_progress.setValue(4)
        translate = self.workflow_page.translate.isChecked()

        def operation(progress: Callable[[str, int, str], None]) -> Any:
            return self.workflow.process_job(
                job_id,
                translate=translate,
                progress=progress,
            )

        task = self._run_background(
            operation,
            on_result=self._job_completed,
            busy_message="正在處理媒體…",
        )
        task.signals.progress.connect(
            lambda _stage, percent, _message: self.engine_progress.setValue(percent)
        )
        task.signals.finished.connect(self._finish_job_ui)

    def _job_completed(self, result: dict[str, Any]) -> None:
        self.segment_model.set_segments(result.get("segments", []))
        outputs = result.get("outputs", [])
        self.workflow_page.append_log(f"已產生 {len(outputs)} 個輸出檔案。")
        self.refresh_project()
        self.tabs.setCurrentWidget(self.editor_page)

    def _finish_job_ui(self) -> None:
        self._running_job_id = None
        self._running_operation = None
        self._refresh_engine_controls()
        self.workflow_page.set_running(False)

    @Slot()
    def cancel_current_job(self) -> None:
        job_id = self._running_job_id
        if job_id is None:
            return
        if self.workflow.cancel_job(job_id):
            if self._running_operation == "speech":
                message = "已送出取消要求；語音合成會在安全邊界停止，轉錄狀態不會被改動。"
            else:
                message = (
                    "已送出取消要求；HearFlow 會終止本機推論程序並重新載入引擎，完成後即可安全重試。"
                    if self.settings.engine.mode == "managed"
                    else "已送出取消要求；外部 Gateway 可能仍會完成本次推論，但較晚結果不會寫入。"
                )
            self.workflow_page.append_log(message)
            self.workflow_page.set_progress("asr", 0, "正在安全取消工作…")

    @Slot()
    def check_environment(self) -> None:
        self.engine_state.setText("檢查中")
        self.engine_detail.setText("正在檢查辨識引擎與媒體工具…")
        self.engine_progress.setRange(0, 0)
        self._run_background(
            lambda _progress: self.workflow.environment_check(),
            on_result=self._environment_ready,
            busy_message="正在檢查環境…",
            show_errors=False,
        )

    def _environment_ready(self, report: dict[str, Any]) -> None:
        engine = report.get("engine", {})
        ready = bool(engine.get("ready"))
        self.engine_state.setText("可使用" if ready else "未就緒")
        models = engine.get("models") or []
        if models:
            self.model_value.setText(", ".join(str(item) for item in models))
        self.engine_detail.setText(str(engine.get("detail") or "Gateway 已連線"))
        self.header_status.setText("● 引擎就緒" if ready else "● 引擎尚未就緒")
        self.header_status.setProperty("state", "ready" if ready else "offline")
        self.header_status.style().unpolish(self.header_status)
        self.header_status.style().polish(self.header_status)
        self.engine_progress.setRange(0, 100)
        self.engine_progress.setValue(100 if ready else 0)
        self._last_environment_report = report

    def _refresh_engine_controls(self) -> None:
        enabled = not self._engine_operation_active and self._running_job_id is None
        for control in (
            self.install_engine_button,
            self.start_engine_button,
            self.stop_engine_button,
            self.install_engine_action,
            self.start_engine_action,
            self.stop_engine_action,
            self.settings_action,
        ):
            control.setEnabled(enabled)

    def _set_engine_operation_active(self, active: bool) -> None:
        self._engine_operation_active = active
        self._refresh_engine_controls()

    def _engine_operation_failed(self) -> None:
        self._set_engine_operation_active(False)
        self.engine_progress.setRange(0, 100)
        self.engine_progress.setValue(0)

    @Slot()
    def quick_install_engine(self) -> None:
        if self._engine_operation_active:
            self.statusBar().showMessage("辨識引擎作業正在進行，請稍候。", 4_000)
            return
        if self._running_job_id is not None:
            QMessageBox.information(self, "工作執行中", "請等待目前工作完成或先取消。")
            return
        if self.settings.engine.mode != "managed":
            QMessageBox.information(
                self,
                "目前使用外部引擎",
                "請先在偏好設定切換為「由聽序管理本機引擎」。",
            )
            return

        def verify(progress: Callable[[str, int, str], None]) -> dict[str, Any]:
            progress("inspect", 5, "正在驗證本機辨識引擎完整性…")
            result = self.engine_manager.verify_installation()
            progress("inspect", 100, str(result.get("integrity_detail") or "驗證完成"))
            return result

        self.engine_progress.setRange(0, 0)
        self._set_engine_operation_active(True)
        task = self._run_background(
            verify,
            on_result=self._complete_quick_install_verification,
            busy_message="正在驗證本機辨識引擎…",
        )
        task.signals.error.connect(lambda _message, _detail: self._engine_operation_failed())

    def _complete_quick_install_verification(self, installation: dict[str, Any]) -> None:
        self._set_engine_operation_active(False)
        if installation.get("integrity_ready") is True:
            self.statusBar().showMessage("本機引擎已包含，正在直接啟動…")
            self.start_local_engine()
            return
        self.engine_progress.setRange(0, 100)
        self.engine_progress.setValue(0)
        script = application_root() / "scripts" / "install-engine.ps1"
        if not script.is_file():
            QMessageBox.critical(
                self,
                "找不到安裝工具",
                f"此發行版本缺少快速安裝工具：\n{script}",
            )
            return
        answer = QMessageBox.question(
            self,
            "快速安裝本機引擎",
            (
                "目前的本機引擎不存在或完整性驗證未通過：\n"
                f"{installation.get('integrity_detail') or '原因不明'}\n\n"
                "將下載 llama.cpp、FFmpeg、Qwen3-ASR 0.6B 模型並建置 Gateway。"
                "\n\n下載量約數 GB，會使用一些時間與磁碟空間。要繼續嗎？"
            ),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        backend = {
            "cuda": "cuda12.4",
            "vulkan": "vulkan",
            "cpu": "cpu",
        }[self.settings.engine.backend]
        runtime = self.engine_manager.runtime_root

        def operation(progress: Callable[[str, int, str], None]) -> str:
            progress("inspect", 5, "正在下載並安裝本機辨識引擎…")
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    "-Backend",
                    backend,
                    "-RuntimeRoot",
                    str(runtime),
                ],
                cwd=str(application_root()),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
            if completed.returncode != 0:
                message = completed.stderr.strip() or completed.stdout.strip()
                raise RuntimeError(
                    "本機引擎安裝失敗。\n"
                    + (message[-1800:] if message else "安裝工具未提供錯誤訊息。")
                )
            progress("inspect", 100, "本機引擎安裝完成")
            return completed.stdout

        self.engine_progress.setRange(0, 0)
        self._set_engine_operation_active(True)
        task = self._run_background(
            operation,
            on_result=lambda _output: self._engine_installed(),
            busy_message="正在安裝本機辨識引擎…",
        )
        task.signals.error.connect(lambda _message, _detail: self._engine_operation_failed())

    def _engine_installed(self) -> None:
        self._set_engine_operation_active(False)
        QMessageBox.information(
            self,
            "安裝完成",
            "本機辨識引擎已安裝。接下來將啟動並檢查服務。",
        )
        self.start_local_engine()

    @Slot()
    def start_local_engine(self) -> None:
        if self._engine_operation_active:
            self.statusBar().showMessage("辨識引擎作業正在進行，請稍候。", 4_000)
            return
        if self._running_job_id is not None:
            QMessageBox.information(self, "工作執行中", "請等待目前工作完成或先取消。")
            return
        if self.settings.engine.mode != "managed":
            QMessageBox.information(
                self,
                "外部 Gateway 模式",
                "外部 Gateway 不由聽序啟動或停止；請直接使用「重新檢查」。",
            )
            return
        status = self.engine_manager.installation_status()
        missing = [
            label
            for label, key in (
                ("llama.cpp", "llama_server_exists"),
                ("Qwen3-ASR Gateway", "gateway_exists"),
                ("FFmpeg", "ffmpeg_exists"),
                ("FFprobe", "ffprobe_exists"),
            )
            if not status.get(key)
        ]
        if missing:
            QMessageBox.warning(
                self,
                "本機引擎尚未安裝完整",
                "缺少：" + "、".join(missing) + "\n\n請先按「快速安裝」。",
            )
            return
        self.engine_state.setText("啟動中")
        self.engine_detail.setText("首次啟動可能需要下載或載入模型，請稍候。")
        self.engine_progress.setRange(0, 0)
        self._set_engine_operation_active(True)

        def operation(progress: Callable[[str, int, str], None]) -> Any:
            progress("inspect", 10, "正在啟動 llama.cpp 與 Qwen3-ASR Gateway…")
            return self.engine_manager.start()

        task = self._run_background(
            operation,
            on_result=lambda _processes: self._engine_started(),
            busy_message="正在啟動本機辨識引擎…",
        )
        task.signals.error.connect(lambda _message, _detail: self._engine_operation_failed())
        task.signals.finished.connect(lambda: self._set_engine_operation_active(False))

    def _engine_started(self) -> None:
        self.workflow.gateway = self.engine_manager.client
        if self._running_job_id is None:
            self.workflow_page.show_engine_ready()
        self.check_environment()

    @Slot()
    def stop_local_engine(self) -> None:
        if self._engine_operation_active:
            self.statusBar().showMessage("辨識引擎作業正在進行，請稍候。", 4_000)
            return
        if self.settings.engine.mode != "managed":
            QMessageBox.information(
                self,
                "外部 Gateway 模式",
                "聽序不會停止不屬於本程式的外部服務。",
            )
            return
        if self._running_job_id is not None:
            QMessageBox.warning(
                self,
                "工作執行中",
                "請先取消目前工作，再停止辨識引擎。",
            )
            return
        self._set_engine_operation_active(True)
        task = self._run_background(
            lambda _progress: self.engine_manager.stop(),
            on_result=lambda _nothing: self._engine_stopped(),
            busy_message="正在停止本機引擎…",
        )
        task.signals.error.connect(lambda _message, _detail: self._engine_operation_failed())
        task.signals.finished.connect(lambda: self._set_engine_operation_active(False))

    def _engine_stopped(self) -> None:
        self.engine_state.setText("已停止")
        self.engine_detail.setText("本機引擎已安全停止。")
        self.header_status.setText("● 引擎已停止")
        self.engine_progress.setRange(0, 100)
        self.engine_progress.setValue(0)

    # ------------------------------------------------------------------
    # Translation engine (TranslateGemma)
    # ------------------------------------------------------------------

    @Slot()
    def start_translation_engine(self) -> None:
        if self._translation_engine_active:
            self.statusBar().showMessage("翻譯引擎作業正在進行，請稍候。", 4_000)
            return
        if not self.settings.translation_engine.enabled:
            QMessageBox.information(
                self,
                "翻譯引擎未啟用",
                "請先在偏好設定啟用翻譯引擎（TranslateGemma）。",
            )
            return
        status = self.translation_engine_manager.installation_status()
        if not status.get("llama_server_exists"):
            QMessageBox.warning(
                self,
                "缺少 llama-server",
                "找不到 llama-server；請先安裝本機辨識引擎。",
            )
            return
        if not status.get("model_ready"):
            QMessageBox.warning(
                self,
                "缺少翻譯模型",
                "找不到 TranslateGemma / Gemma-3-4B-IT 模型。\n"
                "請執行 .\\scripts\\install-engine.ps1 -InstallTranslationModel",
            )
            return
        self.te_state.setText("啟動中")
        self.te_detail.setText("正在載入 TranslateGemma 4B 模型…")
        self.te_progress.setRange(0, 0)
        self._translation_engine_active = True

        def operation(progress: Callable[[str, int, str], None]) -> Any:
            progress("inspect", 10, "正在啟動翻譯引擎…")
            return self.translation_engine_manager.start()

        task = self._run_background(
            operation,
            on_result=lambda _proc: self._translation_engine_started(),
            busy_message="正在啟動翻譯引擎…",
        )
        task.signals.error.connect(self._translation_engine_failed)
        task.signals.finished.connect(lambda: self._set_te_operation_active(False))

    def _translation_engine_started(self) -> None:
        self.te_state.setText("可使用")
        self.te_detail.setText(
            f"TranslateGemma 就緒 — {self.translation_engine_manager.base_url}"
        )
        self.te_progress.setRange(0, 100)
        self.te_progress.setValue(100)
        self.statusBar().showMessage("翻譯引擎已就緒", 5_000)

    def _translation_engine_failed(self, message: str, _detail: str) -> None:
        self._set_te_operation_active(False)
        self.te_state.setText("啟動失敗")
        self.te_detail.setText(message)
        self.te_progress.setRange(0, 100)
        self.te_progress.setValue(0)

    def _set_te_operation_active(self, active: bool) -> None:
        self._translation_engine_active = active
        self.start_te_button.setEnabled(not active)
        self.stop_te_button.setEnabled(not active)

    @Slot()
    def stop_translation_engine(self) -> None:
        if self._translation_engine_active:
            self.statusBar().showMessage("翻譯引擎作業正在進行，請稍候。", 4_000)
            return
        self._set_te_operation_active(True)
        task = self._run_background(
            lambda _progress: self.translation_engine_manager.stop(),
            on_result=lambda _nothing: self._translation_engine_stopped(),
            busy_message="正在停止翻譯引擎…",
        )
        task.signals.error.connect(self._translation_engine_failed)
        task.signals.finished.connect(lambda: self._set_te_operation_active(False))

    def _translation_engine_stopped(self) -> None:
        self.te_state.setText("已停止")
        self.te_detail.setText("翻譯引擎已安全停止。")
        self.te_progress.setRange(0, 100)
        self.te_progress.setValue(0)

    @Slot()
    def check_translation_engine(self) -> None:
        if not self.settings.translation_engine.enabled:
            self.te_state.setText("未啟用")
            self.te_detail.setText("翻譯引擎未啟用。")
            return
        self.te_state.setText("檢查中")
        self.te_progress.setRange(0, 0)

        def operation(_progress: Callable[[str, int, str], None]) -> dict[str, Any]:
            return self.translation_engine_manager.health()

        self._run_background(
            operation,
            on_result=self._translation_engine_health_ready,
            busy_message="正在檢查翻譯引擎…",
            show_errors=False,
        )

    def _translation_engine_health_ready(self, report: dict[str, Any]) -> None:
        ready = bool(report.get("ready"))
        self.te_state.setText("可使用" if ready else "未就緒")
        self.te_detail.setText(
            f"TranslateGemma 就緒 — port {report.get('port')}"
            if ready
            else "翻譯引擎尚未啟動或無法連線。"
        )
        self.te_progress.setRange(0, 100)
        self.te_progress.setValue(100 if ready else 0)

    @Slot()
    def save_segments(self) -> None:
        repository = self.workflow.repository
        if repository is None or self._selected_job_id is None:
            return
        try:
            repository.replace_segments(self._selected_job_id, self.segment_model.segments)
            repository.export_summary()
        except Exception as exc:
            QMessageBox.critical(self, "無法儲存", str(exc))
            return
        self.statusBar().showMessage("字幕編輯已儲存", 4_000)

    @Slot(int)
    def split_segment(self, row: int) -> None:
        segment = self.segment_model.segment_at(row)
        if segment is None or len(segment.source_text) < 2:
            return
        position = max(1, len(segment.source_text) // 2)
        try:
            result = self.workflow.subtitles.split(
                self.segment_model.segments,
                segment.id,
                position=position,
            )
        except Exception as exc:
            QMessageBox.warning(self, "無法分段", str(exc))
            return
        self.segment_model.set_segments(result)
        self.save_segments()

    @Slot(int)
    def merge_segment(self, row: int) -> None:
        first = self.segment_model.segment_at(row)
        second = self.segment_model.segment_at(row + 1)
        if first is None or second is None:
            QMessageBox.information(self, "無法合併", "選取的字幕後方沒有下一段。")
            return
        try:
            result = self.workflow.subtitles.merge(self.segment_model.segments, first.id, second.id)
        except Exception as exc:
            QMessageBox.warning(self, "無法合併", str(exc))
            return
        self.segment_model.set_segments(result)
        self.save_segments()

    @Slot(str)
    def export_selected_format(self, extension: str) -> None:
        repository = self.workflow.repository
        if repository is None or self._selected_job_id is None:
            QMessageBox.information(self, "尚未選取", "請先選擇有字幕內容的媒體。")
            return
        segments = self.segment_model.segments
        if not segments:
            QMessageBox.information(self, "沒有字幕", "目前項目尚未產生字幕。")
            return
        job = repository.get_job(self._selected_job_id)
        manifest = repository.manifest()
        destination = repository.root / "exports" / f"{job.source_path.stem}.{extension}"

        def operation(_progress: Callable[[str, int, str], None]) -> Path:
            output = self.workflow.subtitles.export(
                segments,
                ExportOptions(
                    output_path=destination,
                    prefer_translation=any(item.translated_text is not None for item in segments),
                    language=manifest.target_language or manifest.source_language,
                    timestamp_accuracy="chunk",
                ),
            )
            repository.add_artifact(job.id, ArtifactKind.EXPORT, output)
            return output

        self._run_background(
            operation,
            on_result=self._export_complete,
            busy_message=f"正在輸出 {extension.upper()}…",
        )

    def _export_complete(self, path: Path) -> None:
        self.refresh_files()
        self.statusBar().showMessage(f"已輸出：{path.name}", 6_000)

    @Slot()
    def synthesize_speech(self) -> None:
        repository = self.workflow.repository
        if repository is None or self._selected_job_id is None:
            QMessageBox.information(self, "尚未選取", "請先選擇有逐字稿的媒體項目。")
            return
        if not self.settings.speech.enabled:
            QMessageBox.information(
                self,
                "TTS 尚未啟用",
                "請先在偏好設定啟用本機 Qwen3-TTS 或遠端 TTS 供應商。",
            )
            return
        if self._running_job_id is not None:
            QMessageBox.information(self, "工作執行中", "請等待目前工作完成或先取消。")
            return
        segments = repository.load_segments(self._selected_job_id)
        if not segments:
            QMessageBox.information(self, "沒有文字", "請先完成轉錄或匯入字幕。")
            return
        job_id = self._selected_job_id
        self._running_job_id = job_id
        self._running_operation = "speech"
        self._refresh_engine_controls()
        self.workflow_page.set_running(True)
        self.workflow_page.set_progress("speech", 5, "正在準備語音合成…")

        task = self._run_background(
            lambda progress: self.workflow.synthesize_job_speech(
                job_id,
                prefer_translation=True,
                progress=progress,
            ),
            on_result=self._speech_complete,
            busy_message="正在產生語音…",
        )
        task.signals.finished.connect(self._finish_job_ui)

    def _speech_complete(self, path: Path) -> None:
        self.refresh_files()
        QMessageBox.information(self, "語音已完成", f"已另存語音檔：\n{path}")

    @Slot()
    def burn_subtitles(self) -> None:
        repository = self.workflow.repository
        if repository is None or self._selected_job_id is None:
            return
        job = repository.get_job(self._selected_job_id)
        if job.metadata is None or not job.metadata.video_streams:
            QMessageBox.information(
                self,
                "無法壓入字幕",
                "目前來源是純音訊，或尚未完成媒體檢查；只有含影像串流的影片可壓入字幕。",
            )
            return
        segments = self.segment_model.segments
        if not segments:
            QMessageBox.information(self, "沒有字幕", "請先完成轉錄或匯入字幕。")
            return
        manifest = repository.manifest()
        ass_destination = repository.root / "exports" / f"{job.source_path.stem}.burn.ass"
        video_destination = self._versioned_output(
            repository.root / "exports" / f"{job.source_path.stem}.subtitled.mp4"
        )

        def operation(progress: Callable[[str, int, str], None]) -> Path:
            progress("export", 12, "正在準備 ASS 字幕…")
            ass_path = self.workflow.subtitles.export(
                segments,
                ExportOptions(
                    output_path=ass_destination,
                    prefer_translation=any(item.translated_text is not None for item in segments),
                    language=manifest.target_language or manifest.source_language,
                    timestamp_accuracy="chunk",
                ),
            )
            repository.add_artifact(job.id, ArtifactKind.EXPORT, ass_path)
            progress("export", 30, "正在將字幕壓入影片；原始影片不會被修改…")
            output = self.workflow.media.burn_ass(
                job.source_path,
                ass_path,
                video_destination,
                overwrite=False,
            )
            repository.add_artifact(job.id, ArtifactKind.BURNED_VIDEO, output)
            progress("complete", 100, "含字幕影片已另存完成")
            return output

        self._run_background(
            operation,
            on_result=self._burn_complete,
            busy_message="正在產生含字幕影片…",
        )

    def _burn_complete(self, path: Path) -> None:
        self.refresh_files()
        QMessageBox.information(
            self,
            "影片已完成",
            f"已另存含字幕影片，原始檔案未被修改：\n{path}",
        )

    @staticmethod
    def _versioned_output(path: Path) -> Path:
        if not path.exists():
            return path
        version = 2
        while True:
            candidate = path.with_name(f"{path.stem}.v{version}{path.suffix}")
            if not candidate.exists():
                return candidate
            version += 1

    @Slot()
    def save_project_settings(self) -> None:
        repository = self.workflow.repository
        if repository is None:
            return
        repository.update_project(
            name=self.project_settings.name.text(),
            source_language=str(self.project_settings.source_language.currentData()),
            target_language=(str(self.project_settings.target_language.currentData()) or None),
            translation_style=self.project_settings.style.currentText(),
        )
        self.statusBar().showMessage("專案設定已儲存", 4_000)

    @Slot()
    def apply_translation_page(self) -> None:
        self.workflow_page.translate.setChecked(self.translation_page.enabled.isChecked())
        if self.workflow.repository is not None:
            self.project_settings.target_language.setCurrentText(
                self.translation_page.target.text()
            )
        self.statusBar().showMessage("翻譯選項已套用到下一次執行", 4_000)

    @Slot()
    def show_settings(self) -> None:
        if self._engine_operation_active:
            self.statusBar().showMessage("辨識引擎作業正在進行，暫時不能變更設定。", 4_000)
            return
        if self._running_job_id is not None:
            QMessageBox.information(self, "工作執行中", "請等待目前工作完成或先取消。")
            return
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        runtime_root = self.engine_manager.runtime_root
        try:
            updated = dialog.build_settings()
            candidate_engine = EngineManager(updated.engine, runtime_root=runtime_root)
            candidate_translation = TranslationEngineManager(
                updated.translation_engine,
                runtime_root=runtime_root,
            )
            candidate_gateway = build_transcription_client(
                updated,
                candidate_engine,
                self.secret_store,
            )
            candidate_translator = build_translator(
                updated,
                self.secret_store,
                candidate_translation,
            )
            candidate_speech = build_speech_synthesizer(
                updated,
                self.secret_store,
                runtime_root=runtime_root,
            )
            for credential_id, secret in dialog.secret_values().items():
                self.secret_store.save_provider_key(credential_id, secret)
            if self.settings_repository is not None:
                self.settings_repository.save(updated)
            self.engine_manager.stop()
            self.translation_engine_manager.stop()
        except Exception as exc:
            QMessageBox.critical(self, "無法儲存設定", str(exc))
            return

        self.settings = updated
        self.engine_manager = candidate_engine
        self.translation_engine_manager = candidate_translation
        self.workflow.gateway = candidate_gateway
        self.workflow.translator = candidate_translator
        self.workflow.speech = SpeechRenderService(
            candidate_speech,
            ffmpeg_bin=str(self.workflow.media.ffmpeg_bin),
        )
        self.workflow.cancel_backend = (
            self.engine_manager.cancel_and_restart if updated.engine.mode == "managed" else None
        )
        self.model_value.setText(updated.engine.model_id)
        self.backend_value.setText(updated.engine.backend.upper())
        self.te_model_value.setText(updated.translation_engine.model_id)
        self.te_backend_value.setText(updated.translation_engine.backend.upper())
        self.te_port_value.setText(str(updated.translation_engine.port))
        self.check_environment()
        self.check_translation_engine()

    @Slot()
    def open_project_folder(self) -> None:
        repository = self.workflow.repository
        if repository is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(repository.root)))

    @Slot()
    def open_selected_source(self) -> None:
        repository = self.workflow.repository
        if repository is None or self._selected_job_id is None:
            return
        source = repository.get_job(self._selected_job_id).source_path
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(source.parent)))

    def refresh_files(self) -> None:
        repository = self.workflow.repository
        if repository is None:
            self.reports_page.set_files([])
            return
        files: list[Path] = []
        for folder in ("subtitles", "exports", "reports"):
            root = repository.root / folder
            if root.exists():
                files.extend(path for path in root.rglob("*") if path.is_file())
        self.reports_page.set_files(sorted(files, key=lambda path: path.name))

    def show_environment_report(self) -> None:
        report = getattr(self, "_last_environment_report", None)
        if report is None:
            self.check_environment()
            return
        EnvironmentReportDialog(report, self).exec()

    @staticmethod
    def _select_combo_data(combo: QComboBox, value: Any) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _about(self) -> None:
        QMessageBox.about(
            self,
            "關於聽序",
            (
                "<b>聽序 HearFlow Studio</b><br><br>"
                "Windows 優先的 Qwen3-ASR 字幕工作站。<br>"
                "辨識、校訂、翻譯、QA 與多格式輸出都在同一個專案內完成。<br><br>"
                "時間戳記精度誠實標示為 chunk（片段級）。"
            ),
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._engine_operation_active:
            QMessageBox.information(
                self,
                "辨識引擎作業進行中",
                "請等待目前的引擎驗證、安裝或啟停完成後再關閉。",
            )
            event.ignore()
            return
        if self._running_job_id is not None:
            answer = QMessageBox.question(
                self,
                "工作仍在執行",
                "要取消目前工作並關閉嗎？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.cancel_current_job()
        self.engine_manager.stop()
        self.translation_engine_manager.stop()
        self.workflow.close_project()
        event.accept()
