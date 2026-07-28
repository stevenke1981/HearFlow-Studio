"""Application workflow that joins persistence, ASR, translation, QA, and export."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from hearflow.domain.models import (
    ArtifactKind,
    AsrResult,
    CleanRules,
    ExportOptions,
    JobStatus,
    MediaJob,
    QaIssue,
    QaRules,
    SubtitleSegment,
    TranslationStatus,
)
from hearflow.services.fsutil import atomic_write_text, safe_filename, sha256_file
from hearflow.services.gateway import (
    CancellationToken,
    QwenGatewayClient,
    RequestCancelled,
    TranscriptionOptions,
)
from hearflow.services.media import MediaInspector, discover_media
from hearflow.services.project_store import ProjectRepository
from hearflow.services.reporting import ReportService
from hearflow.services.subtitles import SubtitleDocumentService, SubtitleQaService
from hearflow.services.translation import DisabledTranslator, Translator

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int, str], None]
ALLOWED_EXPORT_FORMATS = frozenset({"srt", "vtt", "ass", "txt", "json"})


class WorkflowError(RuntimeError):
    """A user-actionable end-to-end workflow failure."""


class StaleResultSuppressed(WorkflowError):
    """Raised when a superseded attempt returns after a newer one began."""


class StudioWorkflow:
    """Own one active project and execute recoverable media jobs."""

    def __init__(
        self,
        *,
        gateway: QwenGatewayClient,
        media: MediaInspector,
        translator: Translator | None = None,
        subtitles: SubtitleDocumentService | None = None,
        qa: SubtitleQaService | None = None,
        reports: ReportService | None = None,
        cancel_backend: Callable[[], None] | None = None,
    ) -> None:
        self.gateway = gateway
        self.media = media
        self.translator: Translator = translator or DisabledTranslator()
        self.subtitles = subtitles or SubtitleDocumentService()
        self.qa = qa or SubtitleQaService()
        self.reports = reports or ReportService()
        self.cancel_backend = cancel_backend
        self.repository: ProjectRepository | None = None
        self._tokens: dict[str, CancellationToken] = {}

    def create_project(
        self,
        root: str | Path,
        name: str,
        *,
        source_language: str = "auto",
        target_language: str | None = "zh-TW",
        translation_style: str = "台灣繁體中文",
    ) -> ProjectRepository:
        self.close_project()
        self.repository = ProjectRepository.create(
            root,
            name,
            source_language=source_language,
            target_language=target_language,
            translation_style=translation_style,
        )
        return self.repository

    def open_project(self, root: str | Path) -> ProjectRepository:
        self.close_project()
        self.repository = ProjectRepository.open(root)
        return self.repository

    def close_project(self) -> None:
        if self._tokens:
            raise WorkflowError("工作仍在執行，請先取消並等待工作結束。")
        if self.repository is not None:
            self.repository.close()
            self.repository = None

    def add_media(self, paths: Iterable[str | Path]) -> list[MediaJob]:
        repository = self._require_repository()
        jobs = []
        for path in discover_media(paths):
            jobs.append(repository.add_media(path, fingerprint=sha256_file(path)))
        return jobs

    def inspect_job(self, job_id: str) -> MediaJob:
        repository = self._require_repository()
        job = repository.get_job(job_id)
        if job.fingerprint is not None and sha256_file(job.source_path) != job.fingerprint:
            message = "來源媒體內容在加入後已變更；請重新加入檔案再執行。"
            repository.set_job_status(
                job_id,
                JobStatus.FAILED,
                error_message=message,
            )
            raise WorkflowError(message)
        repository.set_job_status(job_id, JobStatus.INSPECTING)
        metadata = self.media.probe(job.source_path)
        repository.database.execute(
            """
            UPDATE media_jobs
               SET metadata_json = ?, size_bytes = ?, status = ?, updated_at = ?
             WHERE id = ?
            """,
            (
                json.dumps(metadata.to_dict(), ensure_ascii=False),
                metadata.size_bytes,
                JobStatus.READY.value,
                _utc(),
                job_id,
            ),
        )
        repository.log_event(
            "info",
            "media_inspected",
            f"媒體掃描完成：{metadata.duration_seconds:.2f} 秒",
            job_id=job_id,
        )
        repository.export_summary()
        return repository.get_job(job_id)

    def process_job(
        self,
        job_id: str,
        *,
        prompt: str = "",
        language: str = "auto",
        translate: bool = False,
        export_formats: tuple[str, ...] = ("srt", "vtt", "ass", "txt", "json"),
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Run a real job and persist each irreversible boundary."""

        repository = self._require_repository()
        notify = progress or (lambda _stage, _percent, _message: None)
        if job_id in self._tokens:
            raise WorkflowError("此媒體工作已在執行，請等待或先取消。")
        requested_formats = tuple(
            format_name.casefold().lstrip(".") for format_name in export_formats
        )
        invalid_formats = sorted(set(requested_formats) - ALLOWED_EXPORT_FORMATS)
        if invalid_formats:
            raise WorkflowError("不支援的輸出格式：" + "、".join(invalid_formats))
        token = CancellationToken()
        self._tokens[job_id] = token
        logger.info("開始處理工作 %s（translate=%s）", job_id, translate)
        issues: list[QaIssue] = []
        output_paths: list[Path] = []
        report_paths: tuple[Path, Path] | None = None
        attempt_id = generation_id = 0
        try:
            job = self.inspect_job(job_id)
            notify("inspect", 8, "媒體檢查完成")
            attempt_id, generation_id = repository.database.begin_attempt(job_id)

            result = self._run_asr(
                repository, job, job_id, attempt_id, generation_id, token,
                language=language, prompt=prompt, notify=notify,
            )
            cleaned = self._run_clean(
                repository, job_id, attempt_id, generation_id, token, result, notify,
            )
            translated_segments, translation_status = self._run_translation(
                repository, job_id, attempt_id, generation_id, token,
                cleaned, translate=translate, notify=notify,
            )
            issues = self._run_qa(
                repository, job_id, attempt_id, generation_id,
                translated_segments, translation_status,
                require_translation=translate, notify=notify,
            )
            output_paths = self._run_export(
                repository, job, job_id, attempt_id, generation_id, token,
                translated_segments, result, requested_formats,
                prefer_translation=translation_status is TranslationStatus.COMPLETED,
                require_translation=translate, notify=notify,
            )
            report_paths = self._run_report(
                repository, job_id, attempt_id, generation_id,
                translated_segments, issues, result, translation_status, notify,
            )
            return {
                "job": repository.get_job(job_id),
                "segments": translated_segments,
                "issues": issues,
                "outputs": output_paths,
                "reports": list(report_paths),
                "asr": result,
            }
        except StaleResultSuppressed as exc:
            if self._tokens.get(job_id) is token and token.cancelled:
                self._finalize_cancel(repository, job_id)
                raise RequestCancelled() from exc
            repository.log_event(
                "info",
                "stale_result_suppressed",
                "較舊的工作結果已忽略，未修改目前工作。",
                job_id=job_id,
                attempt_id=attempt_id or None,
                generation_id=generation_id or None,
            )
            raise
        except RequestCancelled:
            if self._tokens.get(job_id) is token:
                self._finalize_cancel(repository, job_id)
            raise
        except BaseException as exc:
            if self._tokens.get(job_id) is token and token.cancelled:
                self._finalize_cancel(repository, job_id)
                raise RequestCancelled() from exc
            current = attempt_id == 0 or _attempt_is_current(
                repository,
                job_id,
                attempt_id,
                generation_id,
            )
            if current:
                repository.set_job_status(
                    job_id,
                    JobStatus.FAILED,
                    error_message=str(exc),
                )
            repository.log_event(
                "error",
                "job_failed" if current else "stale_error_suppressed",
                str(exc) if current else "較舊工作的錯誤已忽略。",
                job_id=job_id,
                details={"type": type(exc).__name__},
                attempt_id=attempt_id or None,
                generation_id=generation_id or None,
            )
            raise
        finally:
            if self._tokens.get(job_id) is token:
                self._tokens.pop(job_id, None)

    # ------------------------------------------------------------------
    # Pipeline stages
    # ------------------------------------------------------------------

    def _run_asr(
        self,
        repository: ProjectRepository,
        job: MediaJob,
        job_id: str,
        attempt_id: int,
        generation_id: int,
        token: CancellationToken,
        *,
        language: str,
        prompt: str,
        notify: ProgressCallback,
    ) -> AsrResult:
        """Transcribe via the ASR gateway and persist the raw response."""

        notify("asr", 15, "正在使用 Qwen3-ASR 轉錄")
        result = self.gateway.transcribe(
            job.source_path,
            TranscriptionOptions(language=language or "auto", prompt=prompt),
            token,
        )
        if not repository.save_transcription(
            job_id, result, attempt_id=attempt_id, generation_id=generation_id,
        ):
            if token.cancelled:
                raise RequestCancelled()
            raise StaleResultSuppressed("較舊的轉錄結果已被安全忽略。")
        raw_path = (
            repository.root
            / "transcripts"
            / (
                f"{safe_filename(job.source_path.stem)}.{job.id[:8]}."
                f"attempt-{attempt_id}.verbose.json"
            )
        )
        atomic_write_text(
            raw_path,
            json.dumps(result.raw_response, ensure_ascii=False, indent=2) + "\n",
        )
        if not repository.add_artifact_if_current(
            job_id,
            ArtifactKind.RAW_RESPONSE,
            raw_path,
            attempt_id=attempt_id,
            generation_id=generation_id,
            metadata={
                "model": result.model,
                "timestamp_accuracy": result.timestamp_accuracy,
                "chunks": result.chunk_count,
            },
        ):
            raw_path.unlink(missing_ok=True)
            _raise_rejected_commit(token, "較舊的原始回應已忽略。")
        return result

    def _run_clean(
        self,
        repository: ProjectRepository,
        job_id: str,
        attempt_id: int,
        generation_id: int,
        token: CancellationToken,
        result: AsrResult,
        notify: ProgressCallback,
    ) -> list[SubtitleSegment]:
        """Clean ASR segments and persist them."""

        notify("clean", 52, "正在清理字幕文字")
        cleaned = self.subtitles.clean(result.segments, CleanRules())
        if not repository.replace_segments_if_current(
            job_id, cleaned, attempt_id=attempt_id, generation_id=generation_id,
        ):
            _raise_rejected_commit(token, "較舊的清理結果已忽略。")
        return cleaned

    def _run_translation(
        self,
        repository: ProjectRepository,
        job_id: str,
        attempt_id: int,
        generation_id: int,
        token: CancellationToken,
        cleaned: list[SubtitleSegment],
        *,
        translate: bool,
        notify: ProgressCallback,
    ) -> tuple[list[SubtitleSegment], TranslationStatus]:
        """Translate segments (or explicitly skip) and persist the outcome."""

        if not repository.set_job_status_if_current(
            job_id,
            JobStatus.TRANSLATING if translate else JobStatus.QA,
            attempt_id=attempt_id,
            generation_id=generation_id,
            translation_status=(
                TranslationStatus.IN_PROGRESS if translate else TranslationStatus.SKIPPED
            ),
        ):
            _raise_rejected_commit(token, "較舊的工作狀態已忽略。")
        outcomes = (
            self.translator.translate(cleaned, token)
            if translate
            else DisabledTranslator().translate(cleaned, token)
        )
        token.raise_if_cancelled()
        if not _attempt_is_current(repository, job_id, attempt_id, generation_id):
            raise StaleResultSuppressed("較舊的翻譯結果已被安全忽略。")
        outcome_by_id = {item.id: item for item in outcomes}
        translated_segments: list[SubtitleSegment] = []
        failed = False
        completed_translation = False
        for item in cleaned:
            outcome = outcome_by_id.get(item.id)
            if outcome is None:
                translated_segments.append(item.clone())
                continue
            failed = failed or outcome.status is TranslationStatus.FAILED
            completed_translation = (
                completed_translation or outcome.status is TranslationStatus.COMPLETED
            )
            translated_segments.append(
                item.clone(
                    translated_text=outcome.text,
                    status=(
                        "translated"
                        if outcome.status is TranslationStatus.COMPLETED
                        else item.status
                    ),
                    metadata={
                        **item.metadata,
                        "translation_status": outcome.status.value,
                        "translation_error": outcome.error,
                    },
                )
            )
        if not repository.replace_segments_if_current(
            job_id, translated_segments, attempt_id=attempt_id, generation_id=generation_id,
        ):
            _raise_rejected_commit(token, "較舊的翻譯結果已忽略。")
        translation_status = (
            TranslationStatus.FAILED
            if failed
            else TranslationStatus.COMPLETED
            if completed_translation
            else TranslationStatus.SKIPPED
        )
        if translate and translation_status is not TranslationStatus.COMPLETED:
            message = (
                "已要求翻譯，但部分或全部片段未成功翻譯；"
                "已保留原文與錯誤資訊，未輸出冒充譯文的字幕。"
            )
            repository.set_job_status_if_current(
                job_id,
                JobStatus.FAILED,
                attempt_id=attempt_id,
                generation_id=generation_id,
                error_message=message,
                translation_status=translation_status,
            )
            raise WorkflowError(message)
        return translated_segments, translation_status

    def _run_qa(
        self,
        repository: ProjectRepository,
        job_id: str,
        attempt_id: int,
        generation_id: int,
        translated_segments: list[SubtitleSegment],
        translation_status: TranslationStatus,
        *,
        require_translation: bool,
        notify: ProgressCallback,
    ) -> list[QaIssue]:
        """Run subtitle QA and reject blocking issues."""

        notify("qa", 68, "正在執行字幕 QA")
        if not repository.set_job_status_if_current(
            job_id,
            JobStatus.QA,
            attempt_id=attempt_id,
            generation_id=generation_id,
            translation_status=translation_status,
        ):
            _raise_rejected_commit(
                CancellationToken(), "較舊的 QA 狀態已忽略。",
            )
        issues = self.qa.inspect(
            translated_segments,
            QaRules(require_translation=require_translation),
        )
        blocking = [issue for issue in issues if issue.blocks_export]
        if blocking:
            raise WorkflowError(f"字幕 QA 發現 {len(blocking)} 個阻擋問題，未執行匯出。")
        return issues

    def _run_export(
        self,
        repository: ProjectRepository,
        job: MediaJob,
        job_id: str,
        attempt_id: int,
        generation_id: int,
        token: CancellationToken,
        translated_segments: list[SubtitleSegment],
        result: AsrResult,
        requested_formats: tuple[str, ...],
        *,
        prefer_translation: bool,
        require_translation: bool,
        notify: ProgressCallback,
    ) -> list[Path]:
        """Export subtitles in every requested format."""

        if not repository.set_job_status_if_current(
            job_id,
            JobStatus.EXPORTING,
            attempt_id=attempt_id,
            generation_id=generation_id,
        ):
            _raise_rejected_commit(token, "較舊的匯出狀態已忽略。")
        notify("export", 80, "正在產生字幕與文字檔")
        output_paths: list[Path] = []
        for extension in requested_formats:
            token.raise_if_cancelled()
            if not _attempt_is_current(repository, job_id, attempt_id, generation_id):
                raise StaleResultSuppressed("較舊的匯出工作已忽略。")
            destination = (
                repository.root
                / "subtitles"
                / (
                    f"{safe_filename(job.source_path.stem)}.{job.id[:8]}."
                    f"attempt-{attempt_id}.{extension}"
                )
            )
            output = self.subtitles.export(
                translated_segments,
                ExportOptions(
                    output_path=destination,
                    prefer_translation=prefer_translation,
                    require_translation=require_translation,
                    language=repository.manifest().target_language or "und",
                    timestamp_accuracy=result.timestamp_accuracy,
                ),
            )
            if not repository.add_artifact_if_current(
                job_id,
                ArtifactKind.SUBTITLE,
                output,
                attempt_id=attempt_id,
                generation_id=generation_id,
                metadata={
                    "format": extension,
                    "timestamp_accuracy": result.timestamp_accuracy,
                },
            ):
                output.unlink(missing_ok=True)
                _raise_rejected_commit(token, "較舊的字幕產物已忽略。")
            output_paths.append(output)
        return output_paths

    def _run_report(
        self,
        repository: ProjectRepository,
        job_id: str,
        attempt_id: int,
        generation_id: int,
        translated_segments: list[SubtitleSegment],
        issues: list[QaIssue],
        result: AsrResult,
        translation_status: TranslationStatus,
        notify: ProgressCallback,
    ) -> tuple[Path, Path]:
        """Mark the job completed, generate reports, and finalise."""

        if not repository.set_job_status_if_current(
            job_id,
            JobStatus.COMPLETED,
            attempt_id=attempt_id,
            generation_id=generation_id,
            translation_status=translation_status,
        ):
            _raise_rejected_commit(
                CancellationToken(), "較舊工作的完成狀態已忽略。",
            )
        finished_job = repository.get_job(job_id)
        report_paths = self.reports.generate_job_report(
            project_name=repository.manifest().name,
            job=finished_job,
            segments=translated_segments,
            issues=issues,
            output_dir=(repository.root / "reports" / f"{finished_job.id[:8]}.attempt-{attempt_id}"),
            engine={
                "model": result.model,
                "chunks": result.chunk_count,
                "elapsed_seconds": result.elapsed_seconds,
                "timestamp_accuracy": result.timestamp_accuracy,
            },
        )
        for report in report_paths:
            if not repository.add_artifact_if_current(
                job_id,
                ArtifactKind.REPORT,
                report,
                attempt_id=attempt_id,
                generation_id=generation_id,
            ):
                report.unlink(missing_ok=True)
                _raise_rejected_commit(
                    CancellationToken(), "較舊的報告產物已忽略。",
                )
        repository.log_event(
            "info",
            "job_completed",
            "完整工作流程已完成",
            job_id=job_id,
            attempt_id=attempt_id,
            generation_id=generation_id,
        )
        repository.export_summary()
        notify("complete", 100, "轉錄、QA 與匯出完成")
        return report_paths

    # ------------------------------------------------------------------
    # Cancellation and environment
    # ------------------------------------------------------------------

    def cancel_job(self, job_id: str) -> bool:
        repository = self._require_repository()
        token = self._tokens.get(job_id)
        if token is None:
            return False
        repository.database.bump_generation(job_id, status=JobStatus.CANCELLING)
        token.cancel()
        return True

    def _finalize_cancel(
        self,
        repository: ProjectRepository,
        job_id: str,
    ) -> None:
        repository.set_job_status(job_id, JobStatus.CANCELLED)
        if self.cancel_backend is not None:
            try:
                self.cancel_backend()
            except Exception as exc:
                repository.log_event(
                    "error",
                    "engine_restart_failed",
                    "工作已取消，但本機引擎重新啟動失敗。",
                    job_id=job_id,
                    details={"type": type(exc).__name__},
                )
        repository.log_event(
            "info",
            "job_cancelled",
            "工作已取消；較晚回傳的結果不會寫入",
            job_id=job_id,
        )

    def environment_check(self) -> dict[str, Any]:
        repository = self.repository
        media_report = self.media.environment_report()
        engine = self.gateway.health()
        result = {
            "ffmpeg": media_report,
            "engine": {
                "live": engine.live,
                "ready": engine.ready,
                "models": list(engine.models),
                "detail": engine.detail,
                "http_status": engine.http_status,
            },
            "project": None,
        }
        if repository is not None:
            result["project"] = {
                "root": str(repository.root),
                "database_integrity": repository.database.integrity_check(),
            }
        return result

    def _require_repository(self) -> ProjectRepository:
        if self.repository is None:
            raise WorkflowError("請先建立或開啟專案。")
        return self.repository


def _utc() -> str:
    from hearflow.domain.models import utc_now_iso

    return utc_now_iso()


def _attempt_is_current(
    repository: ProjectRepository,
    job_id: str,
    attempt_id: int,
    generation_id: int,
) -> bool:
    row = repository.database.fetchone(
        "SELECT attempt_id, generation_id FROM media_jobs WHERE id = ?",
        (job_id,),
    )
    return bool(
        row is not None
        and int(row["attempt_id"]) == attempt_id
        and int(row["generation_id"]) == generation_id
    )


def _raise_rejected_commit(
    token: CancellationToken,
    message: str,
) -> None:
    if token.cancelled:
        raise RequestCancelled()
    raise StaleResultSuppressed(message)
