"""Small user-facing dialogs used by the desktop shell."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from hearflow.services.settings import AppSettings, EngineSettings, TranslationSettings


class NewProjectDialog(QDialog):
    """Collect a project name and parent directory."""

    def __init__(self, start_dir: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("建立新專案")
        self.setMinimumWidth(560)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：訪談字幕")
        self.folder_edit = QLineEdit(start_dir)
        browse = QPushButton("瀏覽…")
        browse.clicked.connect(self._browse)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)
        folder_widget = QWidget()
        folder_widget.setLayout(folder_row)
        form = QFormLayout()
        form.addRow("專案名稱", self.name_edit)
        form.addRow("存放位置", folder_widget)
        hint = QLabel("程式會在選擇的位置內建立專案資料夾，不會修改原始媒體。")
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    @property
    def values(self) -> tuple[Path, str]:
        return Path(self.folder_edit.text()).expanduser(), self.name_edit.text().strip()

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "選擇專案存放位置",
            self.folder_edit.text(),
        )
        if folder:
            self.folder_edit.setText(folder)

    def _accept(self) -> None:
        folder, name = self.values
        if not name:
            QMessageBox.warning(self, "尚未完成", "請輸入專案名稱。")
            return
        if not folder.exists() or not folder.is_dir():
            QMessageBox.warning(self, "尚未完成", "請選擇已存在的存放位置。")
            return
        self.accept()


class SettingsDialog(QDialog):
    """Edit persisted non-secret engine and translation preferences."""

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._original = settings
        self.setWindowTitle("偏好設定")
        self.setMinimumWidth(650)

        self.mode = QComboBox()
        self.mode.addItem("由聽序管理本機引擎", "managed")
        self.mode.addItem("連接既有 Gateway", "external")
        self.mode.setCurrentIndex(max(0, self.mode.findData(settings.engine.mode)))
        self.backend = QComboBox()
        for label, value in (
            ("NVIDIA CUDA", "cuda"),
            ("Vulkan", "vulkan"),
            ("CPU", "cpu"),
        ):
            self.backend.addItem(label, value)
        self.backend.setCurrentIndex(max(0, self.backend.findData(settings.engine.backend)))
        self.gateway_url = QLineEdit(settings.engine.gateway_url)
        self.gateway_key = QLineEdit()
        self.gateway_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.gateway_key.setPlaceholderText("只在本次執行使用，也可設 HEARFLOW_GATEWAY_API_KEY")
        self.model = QLineEdit(settings.engine.model_id)
        self.threads = QSpinBox()
        self.threads.setRange(1, 128)
        self.threads.setValue(settings.engine.threads)

        engine_form = QFormLayout()
        engine_form.addRow("引擎模式", self.mode)
        engine_form.addRow("運算後端", self.backend)
        engine_form.addRow("Gateway 位址", self.gateway_url)
        engine_form.addRow("本次連線金鑰", self.gateway_key)
        engine_form.addRow("ASR 模型", self.model)
        engine_form.addRow("CPU 執行緒", self.threads)

        self.translation_enabled = QCheckBox("啟用字幕翻譯")
        self.translation_enabled.setChecked(settings.translation.enabled)
        self.translation_provider_id = QLineEdit(settings.translation.provider_id)
        self.translation_url = QLineEdit(settings.translation.base_url)
        self.translation_model = QLineEdit(settings.translation.model)
        self.translation_api_key = QLineEdit()
        self.translation_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.translation_api_key.setPlaceholderText("留空則保留既有金鑰；本機 Ollama 通常不需要")
        self.target_language = QLineEdit(settings.translation.target_language)
        self.style = QLineEdit(settings.translation.style)
        translation_form = QFormLayout()
        translation_form.addRow("", self.translation_enabled)
        translation_form.addRow("認證名稱", self.translation_provider_id)
        translation_form.addRow("相容服務位址", self.translation_url)
        translation_form.addRow("翻譯模型", self.translation_model)
        translation_form.addRow("API 金鑰", self.translation_api_key)
        translation_form.addRow("目標語言", self.target_language)
        translation_form.addRow("語氣風格", self.style)

        warning = QLabel("API 金鑰不會寫入一般設定檔；正式翻譯金鑰使用 Windows 認證管理員。")
        warning.setWordWrap(True)
        warning.setProperty("warning", True)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("語音辨識引擎"))
        layout.addLayout(engine_form)
        layout.addWidget(QLabel("翻譯服務"))
        layout.addLayout(translation_form)
        layout.addWidget(warning)
        layout.addWidget(buttons)

    def build_settings(self) -> AppSettings:
        engine: EngineSettings = replace(
            self._original.engine,
            mode=str(self.mode.currentData()),
            backend=str(self.backend.currentData()),
            gateway_url=self.gateway_url.text().strip(),
            model_id=self.model.text().strip(),
            threads=self.threads.value(),
        )
        translation: TranslationSettings = replace(
            self._original.translation,
            enabled=self.translation_enabled.isChecked(),
            provider_id=self.translation_provider_id.text().strip(),
            base_url=self.translation_url.text().strip(),
            model=self.translation_model.text().strip(),
            target_language=self.target_language.text().strip(),
            style=self.style.text().strip(),
        )
        return replace(self._original, engine=engine, translation=translation)


class EnvironmentReportDialog(QDialog):
    """Show an environment report in both plain and copyable form."""

    def __init__(self, report: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("環境檢查結果")
        self.resize(720, 520)
        engine = report.get("engine", {})
        ffmpeg = report.get("ffmpeg", {})
        ready = bool(engine.get("ready"))
        summary = QLabel(
            ("✓ 引擎可用" if ready else "⚠ 引擎尚未就緒")
            + "　"
            + ("✓ FFmpeg 可用" if ffmpeg.get("ffmpeg_exists") else "⚠ 找不到 FFmpeg")
        )
        summary.setProperty("success" if ready else "warning", True)
        detail = QTextEdit()
        detail.setReadOnly(True)
        detail.setPlainText(json.dumps(report, ensure_ascii=False, indent=2))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(summary)
        layout.addWidget(detail, 1)
        layout.addWidget(buttons, alignment=Qt.AlignmentFlag.AlignRight)
