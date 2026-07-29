from __future__ import annotations

import importlib.util
import os
import threading
from collections.abc import Callable
from typing import Protocol

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class _QtBot(Protocol):
    def addWidget(self, widget: object) -> None: ...

    def wait(self, ms: int) -> None: ...

    def waitUntil(self, callback: Callable[[], bool], timeout: int = 5000) -> None: ...


@pytest.mark.skipif(
    importlib.util.find_spec("hearflow.ui.main_window") is None,
    reason="UI module is still being integrated",
)
def test_main_window_can_construct_offscreen(qtbot: _QtBot) -> None:
    from hearflow.application.workflow import StudioWorkflow
    from hearflow.domain.models import EngineStatus
    from hearflow.ui.main_window import MainWindow

    class Gateway:
        def health(self) -> EngineStatus:
            return EngineStatus(live=False, ready=False, detail="離屏測試")

    class Media:
        def environment_report(self) -> dict[str, object]:
            return {"ready": False, "ffmpeg": "test", "ffprobe": "test"}

    class Secrets:
        def load_provider_key(self, _provider_id: str) -> None:
            return None

    workflow = StudioWorkflow(
        gateway=Gateway(),  # type: ignore[arg-type]
        media=Media(),  # type: ignore[arg-type]
    )
    window = MainWindow(workflow, secret_store=Secrets())  # type: ignore[arg-type]
    qtbot.addWidget(window)
    assert window.windowTitle() == "聽序 HearFlow Studio v0.2.0"
    assert window.tabs.count() == 7
    qtbot.wait(400)
    qtbot.waitUntil(lambda: not window._active_tasks)


def test_main_window_controls_remain_accessible_at_1280x720(
    qtbot: _QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication

    from hearflow.application.workflow import StudioWorkflow
    from hearflow.domain.models import EngineStatus
    from hearflow.ui.main_window import MainWindow

    class Gateway:
        def health(self) -> EngineStatus:
            return EngineStatus(live=False, ready=False, detail="離屏測試")

    class Media:
        def environment_report(self) -> dict[str, object]:
            return {"ready": False, "ffmpeg": "test", "ffprobe": "test"}

    class Secrets:
        def load_provider_key(self, _provider_id: str) -> None:
            return None

    workflow = StudioWorkflow(
        gateway=Gateway(),  # type: ignore[arg-type]
        media=Media(),  # type: ignore[arg-type]
    )
    window = MainWindow(workflow, secret_store=Secrets())  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window.resize(1280, 720)
    window.show()
    QApplication.processEvents()

    content = window.content_scroll.widget()
    assert content is not None
    assert window.content_scroll.horizontalScrollBar().maximum() == 0
    assert window.workflow_page.splitter.sizes()[2] >= 260
    window.workflow_page.set_progress("inspect", 10, "正在啟動引擎…")
    window.workflow_page.show_engine_ready()
    assert window.workflow_page.progress.value() == 0
    assert "已就緒" in window.workflow_page.progress.format()
    assert window.workflow_page.stage_list.item(0).text().startswith("✓")
    assert "尚未執行" in window.workflow_page.stage_list.item(1).text()
    window.workflow_page.set_progress("asr", 42, "正在辨識真實工作…")
    progress_before = window.workflow_page.progress.format()
    stage_before = window.workflow_page.stage_list.item(1).text()
    window._running_job_id = "running-job"
    window._refresh_engine_controls()
    monkeypatch.setattr(window, "check_environment", lambda: None)
    window._engine_started()
    assert window.workflow_page.progress.format() == progress_before
    assert window.workflow_page.stage_list.item(1).text() == stage_before
    assert window.start_engine_button.isEnabled() is False
    assert window.settings_action.isEnabled() is False
    window._running_job_id = None
    window._refresh_engine_controls()

    for control in (
        window.install_engine_button,
        window.start_engine_button,
        window.stop_engine_button,
        window.project_settings.source_language,
        window.project_settings.target_language,
        window.workflow_page.log,
    ):
        top_left = control.mapTo(content, QPoint(0, 0))
        assert top_left.x() >= 0
        assert top_left.x() + control.width() <= content.width()

    for index in range(window.tabs.count()):
        window.tabs.setCurrentIndex(index)
        QApplication.processEvents()
        assert window.content_scroll.horizontalScrollBar().maximum() == 0

    verification_gate = threading.Event()
    verification_calls: list[bool] = []
    started: list[bool] = []

    def verify_installation() -> dict[str, bool]:
        verification_calls.append(True)
        assert verification_gate.wait(5)
        return {"integrity_ready": True}

    monkeypatch.setattr(
        window.engine_manager,
        "verify_installation",
        verify_installation,
    )
    monkeypatch.setattr(window, "start_local_engine", lambda: started.append(True))
    window.quick_install_engine()
    qtbot.waitUntil(lambda: verification_calls == [True])
    assert window._engine_operation_active is True
    for control in (
        window.install_engine_button,
        window.start_engine_button,
        window.stop_engine_button,
        window.install_engine_action,
        window.start_engine_action,
        window.stop_engine_action,
        window.settings_action,
    ):
        assert control.isEnabled() is False

    window.quick_install_engine()
    MainWindow.start_local_engine(window)
    assert verification_calls == [True]
    assert started == []
    verification_gate.set()
    qtbot.waitUntil(lambda: started == [True])
    assert started == [True]
    assert window._engine_operation_active is False
    qtbot.waitUntil(lambda: not window._active_tasks)
