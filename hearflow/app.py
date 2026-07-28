"""HearFlow Studio desktop application entry point."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from hearflow.application.workflow import StudioWorkflow
from hearflow.services.engine import EngineManager
from hearflow.services.gateway import QwenGatewayClient
from hearflow.services.media import MediaInspector
from hearflow.services.settings import (
    AppSettings,
    SecretStore,
    SettingsError,
    SettingsRepository,
)
from hearflow.services.translation import DisabledTranslator, OpenAICompatibleTranslator
from hearflow.ui.main_window import MainWindow

logger = logging.getLogger(__name__)


def build_workflow(
    settings: AppSettings,
    engine_manager: EngineManager | None = None,
    secret_store: SecretStore | None = None,
) -> StudioWorkflow:
    """Build production services from persisted, non-secret settings."""

    manager = engine_manager or EngineManager(settings.engine)
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
    if settings.engine.mode == "managed":
        gateway = manager.client
    else:
        gateway_key = os.environ.get("HEARFLOW_GATEWAY_API_KEY")
        gateway = QwenGatewayClient(
            settings.engine.gateway_url,
            api_key=gateway_key,
            timeout=120.0,
        )
    translator = DisabledTranslator()
    if settings.translation.enabled:
        translator = OpenAICompatibleTranslator(
            settings.translation,
            secret_store or SecretStore(),
        )
    return StudioWorkflow(
        gateway=gateway,
        media=MediaInspector(ffmpeg_bin=ffmpeg, ffprobe_bin=ffprobe),
        translator=translator,
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
    app.setApplicationVersion("0.1.0")
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
        workflow = build_workflow(settings, engine_manager, secret_store)
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
