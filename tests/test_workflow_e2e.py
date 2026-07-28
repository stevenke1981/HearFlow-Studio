from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from hearflow.application.workflow import StudioWorkflow, WorkflowError
from hearflow.domain.models import (
    AsrResult,
    JobStatus,
    MediaMetadata,
    SubtitleSegment,
    TranslationStatus,
)
from hearflow.services.gateway import (
    CancellationToken,
    RequestCancelled,
    TranscriptionOptions,
)
from hearflow.services.translation import DisabledTranslator, Translation


class _Gateway:
    def __init__(self, result: AsrResult) -> None:
        self.result = result
        self.calls: list[tuple[Path, TranscriptionOptions]] = []

    def transcribe(
        self,
        path: Path,
        options: TranscriptionOptions,
        cancellation: CancellationToken,
    ) -> AsrResult:
        cancellation.raise_if_cancelled()
        self.calls.append((Path(path), options))
        return self.result


class _Media:
    def probe(self, path: Path) -> MediaMetadata:
        source = Path(path)
        return MediaMetadata(
            path=source,
            duration_seconds=2.8,
            format_name="wav",
            audio_streams=({"codec_name": "pcm_s16le"},),
            size_bytes=source.stat().st_size,
            bit_rate=256_000,
        )

    def environment_report(self) -> dict[str, object]:
        return {"ffmpeg": "stub", "ffprobe": "stub", "ready": True}


class _BlockingGateway:
    def transcribe(
        self,
        path: Path,
        options: TranscriptionOptions,
        cancellation: CancellationToken,
    ) -> AsrResult:
        while not cancellation.cancelled:
            time.sleep(0.005)
        raise RequestCancelled()


class _Translator:
    def translate(
        self,
        segments: list[SubtitleSegment],
        cancellation: CancellationToken,
    ) -> list[Translation]:
        cancellation.raise_if_cancelled()
        return [
            Translation(
                id=segment.id,
                text=f"譯文 {index + 1}",
                status=TranslationStatus.COMPLETED,
                attempts=1,
            )
            for index, segment in enumerate(segments)
        ]


class _MustNotRunTranslator:
    def translate(
        self,
        segments: list[SubtitleSegment],
        cancellation: CancellationToken,
    ) -> list[Translation]:
        raise AssertionError("translate=False must never call the configured provider")


def test_workflow_persists_and_exports_through_real_sqlite(
    tmp_path: Path,
    media_file: Path,
    asr_result: AsrResult,
) -> None:
    gateway = _Gateway(asr_result)
    workflow = StudioWorkflow(
        gateway=gateway,  # type: ignore[arg-type]
        media=_Media(),  # type: ignore[arg-type]
        translator=DisabledTranslator(),
    )
    repository = workflow.create_project(
        tmp_path / "工作流 專案",
        "工作流整合測試",
    )
    jobs = workflow.add_media([media_file])
    progress: list[tuple[str, int, str]] = []
    result = workflow.process_job(
        jobs[0].id,
        prompt="專有名詞",
        language="auto",
        translate=False,
        export_formats=("srt", "vtt", "ass", "txt", "json"),
        progress=lambda stage, percent, message: progress.append((stage, percent, message)),
    )

    completed = repository.get_job(jobs[0].id)
    assert completed.status is JobStatus.COMPLETED
    assert completed.translation_status is TranslationStatus.SKIPPED
    assert completed.attempt_id == 1
    assert completed.generation_id == 0
    assert [item.translated_text for item in result["segments"]] == [None, None]
    assert len(result["outputs"]) == 5
    assert all(path.is_file() for path in result["outputs"])
    assert len(result["reports"]) == 2
    assert all(path.is_file() for path in result["reports"])
    raw_files = list((repository.root / "transcripts").glob("測試 聲音.*.attempt-1.verbose.json"))
    assert len(raw_files) == 1
    assert repository.database.integrity_check()
    assert progress[0][0] == "inspect"
    assert progress[-1][:2] == ("complete", 100)
    assert gateway.calls[0][1].prompt == "專有名詞"
    assert repository.list_events()[-1]["event_type"] == "job_completed"
    workflow.close_project()


def test_workflow_translate_false_never_calls_configured_provider(
    tmp_path: Path,
    media_file: Path,
    asr_result: AsrResult,
) -> None:
    workflow = StudioWorkflow(
        gateway=_Gateway(asr_result),  # type: ignore[arg-type]
        media=_Media(),  # type: ignore[arg-type]
        translator=_MustNotRunTranslator(),  # type: ignore[arg-type]
    )
    workflow.create_project(tmp_path / "private", "不外送")
    job = workflow.add_media([media_file])[0]

    result = workflow.process_job(job.id, translate=False, export_formats=("srt",))

    assert result["job"].translation_status is TranslationStatus.SKIPPED
    workflow.close_project()


def test_workflow_rejects_export_path_traversal_before_processing(
    tmp_path: Path,
    media_file: Path,
    asr_result: AsrResult,
) -> None:
    workflow = StudioWorkflow(
        gateway=_Gateway(asr_result),  # type: ignore[arg-type]
        media=_Media(),  # type: ignore[arg-type]
    )
    repository = workflow.create_project(tmp_path / "safe", "輸出邊界")
    job = workflow.add_media([media_file])[0]

    with pytest.raises(WorkflowError, match="不支援的輸出格式"):
        workflow.process_job(
            job.id,
            translate=False,
            export_formats=("../../outside.srt",),
        )

    assert repository.get_job(job.id).status is JobStatus.PENDING
    assert not (tmp_path / "outside.srt").exists()
    workflow.close_project()


def test_cancel_invalidates_generation_and_finishes_cancelled(
    tmp_path: Path,
    media_file: Path,
) -> None:
    backend_restarted = threading.Event()
    workflow = StudioWorkflow(
        gateway=_BlockingGateway(),  # type: ignore[arg-type]
        media=_Media(),  # type: ignore[arg-type]
        cancel_backend=backend_restarted.set,
    )
    repository = workflow.create_project(tmp_path / "cancel", "取消測試")
    job = workflow.add_media([media_file])[0]
    observed: list[BaseException] = []

    def run() -> None:
        try:
            workflow.process_job(job.id)
        except BaseException as exc:
            observed.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 2
    while job.id not in workflow._tokens and time.monotonic() < deadline:  # noqa: SLF001
        time.sleep(0.005)

    assert workflow.cancel_job(job.id)
    worker.join(timeout=2)

    current = repository.get_job(job.id)
    assert not worker.is_alive()
    assert isinstance(observed[0], RequestCancelled)
    assert current.status is JobStatus.CANCELLED
    assert current.generation_id == 1
    assert backend_restarted.is_set()
    workflow.close_project()


def test_workflow_translation_is_persisted_and_selected_for_export(
    tmp_path: Path,
    media_file: Path,
    asr_result: AsrResult,
) -> None:
    workflow = StudioWorkflow(
        gateway=_Gateway(asr_result),  # type: ignore[arg-type]
        media=_Media(),  # type: ignore[arg-type]
        translator=_Translator(),  # type: ignore[arg-type]
    )
    repository = workflow.create_project(tmp_path / "translated", "翻譯整合測試")
    job = workflow.add_media([media_file])[0]

    result = workflow.process_job(
        job.id,
        translate=True,
        export_formats=("srt",),
    )

    completed = repository.get_job(job.id)
    persisted = repository.load_segments(job.id)
    assert completed.status is JobStatus.COMPLETED
    assert completed.translation_status is TranslationStatus.COMPLETED
    assert [item.translated_text for item in persisted] == ["譯文 1", "譯文 2"]
    exported = result["outputs"][0].read_text("utf-8")
    assert "譯文 1" in exported and "Hello world." not in exported
    workflow.close_project()
