from __future__ import annotations

import json
from pathlib import Path

from hearflow.domain.models import AsrResult, JobStatus, SubtitleSegment
from hearflow.services.database import SCHEMA_VERSION
from hearflow.services.project_store import ProjectRepository


def _replacement_result(original: AsrResult, text: str) -> AsrResult:
    return AsrResult(
        raw_response={**original.raw_response, "text": text},
        response_headers=dict(original.response_headers),
        text=text,
        language=original.language,
        duration=original.duration,
        segments=[
            SubtitleSegment(
                id="late",
                start_ms=0,
                end_ms=900,
                source_text=text,
                metadata={"timestamp_accuracy": "chunk"},
            )
        ],
        model=original.model,
        chunk_count=1,
        timestamp_accuracy="chunk",
    )


def test_project_summary_is_derived_from_sqlite_and_media_is_idempotent(
    tmp_path: Path,
    media_file: Path,
) -> None:
    root = tmp_path / "專案 A"
    repository = ProjectRepository.create(root, "我的字幕專案")
    try:
        first = repository.add_media(media_file, fingerprint="abc")
        second = repository.add_media(media_file, fingerprint="different")

        assert first.id == second.id
        assert second.fingerprint == "different"
        assert second.generation_id == 1
        assert second.status is JobStatus.PENDING
        assert repository.database.integrity_check()
        assert repository.database.schema_version == SCHEMA_VERSION
        summary = json.loads((root / "project.hearflow.json").read_text("utf-8"))
        assert summary["name"] == "我的字幕專案"
        assert len(summary["jobs"]) == 1
        assert summary["jobs"][0]["source_path"] == str(media_file.resolve())
    finally:
        repository.close()

    reopened = ProjectRepository.open(root)
    try:
        assert reopened.manifest().name == "我的字幕專案"
        assert reopened.get_job(first.id).source_path == media_file.resolve()
    finally:
        reopened.close()


def test_attempt_generation_cas_rejects_late_transcription(
    tmp_path: Path,
    media_file: Path,
    asr_result: AsrResult,
) -> None:
    repository = ProjectRepository.create(tmp_path / "CAS", "CAS")
    try:
        job = repository.add_media(media_file)
        attempt, generation = repository.database.begin_attempt(job.id)
        assert (attempt, generation) == (1, 0)
        assert repository.save_transcription(
            job.id,
            asr_result,
            attempt_id=attempt,
            generation_id=generation,
        )
        assert [item.source_text for item in repository.load_segments(job.id)] == [
            "Hello world.",
            "字幕測試！",
        ]

        assert repository.database.bump_generation(job.id) == 1
        stale = _replacement_result(asr_result, "晚到結果不可覆寫")
        assert not repository.save_transcription(
            job.id,
            stale,
            attempt_id=attempt,
            generation_id=generation,
        )
        assert not repository.replace_segments_if_current(
            job.id,
            stale.segments,
            attempt_id=attempt,
            generation_id=generation,
        )
        assert repository.get_job(job.id).status is JobStatus.CANCELLED
        assert [item.source_text for item in repository.load_segments(job.id)] == [
            "Hello world.",
            "字幕測試！",
        ]
        assert repository.list_events()[-1]["event_type"] == "suppressed_stale_result"
    finally:
        repository.close()


def test_open_recovers_interrupted_job(
    tmp_path: Path,
    media_file: Path,
) -> None:
    root = tmp_path / "recovery"
    repository = ProjectRepository.create(root, "復原測試")
    job = repository.add_media(media_file)
    repository.set_job_status(job.id, JobStatus.TRANSLATING)
    repository.close()

    reopened = ProjectRepository.open(root)
    try:
        recovered = reopened.get_job(job.id)
        assert recovered.status is JobStatus.FAILED
        assert recovered.error_message == "上次執行中斷，可安全重試。"
    finally:
        reopened.close()
