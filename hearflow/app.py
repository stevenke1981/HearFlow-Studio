"""HearFlow Studio desktop application entry point."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from hearflow.application.workflow import StudioWorkflow
from hearflow.services.engine import EngineManager
from hearflow.services.media import MediaInspector
from hearflow.services.service_factory import (
    build_speech_synthesizer,
    build_transcription_client,
    build_translator,
)
from hearflow.services.settings import (
    AppSettings,
    SecretStore,
    SettingsError,
    SettingsRepository,
)
from hearflow.services.translation_engine import TranslationEngineManager
from hearflow.ui.main_window import MainWindow

logger = logging.getLogger(__name__)


def build_workflow(
    settings: AppSettings,
    engine_manager: EngineManager | None = None,
    secret_store: SecretStore | None = None,
    translation_engine_manager: TranslationEngineManager | None = None,
) -> StudioWorkflow:
    """Build local/remote services from persisted non-secret settings."""

    manager = engine_manager or EngineManager(settings.engine)
    secrets = secret_store or SecretStore()
    translation_manager = translation_engine_manager or TranslationEngineManager(
        settings.translation_engine,
        runtime_root=manager.runtime_root,
    )
    paths = manager.resolve_paths()
    bundled_ffmpeg = manager.runtime_root / "ffmpeg" / "ffmpeg.exe"
    bundled_ffprobe = manager.runtime_root / "ffmpeg" / "ffprobe.exe"
    ffmpeg = str(
        paths.ffmpeg
        if paths.ffmpeg.is_file()
        else bundled_ffmpeg
        if bundled_ffmpeg.is_file()
        else "ffmpeg"
    )
    ffprobe = str(
        paths.ffprobe
        if paths.ffprobe.is_file()
        else bundled_ffprobe
        if bundled_ffprobe.is_file()
        else "ffprobe"
    )
    media = MediaInspector(ffmpeg_bin=ffmpeg, ffprobe_bin=ffprobe)
    transcription = build_transcription_client(settings, manager, secrets)
    translator = build_translator(settings, secrets, translation_manager)
    speech = build_speech_synthesizer(
        settings,
        secrets,
        runtime_root=manager.runtime_root,
    )
    return StudioWorkflow(
        gateway=transcription,
        media=media,
        translator=translator,
        speech=speech,
        cancel_backend=(manager.cancel_and_restart if settings.engine.mode == "managed" else None),
    )


def load_theme() -> str:
    """Load the packaged Qt stylesheet."""

    path = Path(__file__).resolve().parent / "ui" / "theme.qss"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def main() -> int:
    """Start the Windows desktop UI."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    app = QApplication(sys.argv)
    app.setApplicationName("聽序 HearFlow Studio")
    app.setOrganizationName("HearFlow")
    app.setApplicationVersion("0.2.0")
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft JhengHei UI", 10))
    app.setStyleSheet(load_theme())

    settings_repository = SettingsRepository()
    settings_error: str | None = None
    try:
        settings = settings_repository.load()
    except SettingsError as exc:
        settings = AppSettings()
        settings_error = str(exc)
    try:
        engine_manager = EngineManager(settings.engine)
        secret_store = SecretStore()
        translation_engine_manager = TranslationEngineManager(
            settings.translation_engine,
            runtime_root=engine_manager.runtime_root,
        )
        workflow = build_workflow(
            settings,
            engine_manager,
            secret_store,
            translation_engine_manager,
        )
    except Exception as exc:
        QMessageBox.critical(
            None,
            "無法啟動聽序",
            f"媒體工具或服務設定不完整：\n{exc}",
        )
        return 2

    window = MainWindow(
        workflow,
        settings,
        settings_repository,
        engine_manager=engine_manager,
        secret_store=secret_store,
        translation_engine_manager=translation_engine_manager,
    )
    window.show()
    if settings_error is not None:
        QMessageBox.warning(
            window,
            "已使用預設設定",
            f"{settings_error}\n\n程式已改用安全的預設設定啟動。",
        )
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
