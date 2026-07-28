"""Project repository backed by the canonical SQLite database."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from hearflow.domain.models import (
    ArtifactKind,
    AsrResult,
    JobStatus,
    MediaJob,
    MediaMetadata,
    ProjectManifest,
    SubtitleSegment,
    TranslationStatus,
    utc_now_iso,
)
from hearflow.domain.validation import validate_project_name, without_secrets
from hearflow.services.database import AttemptGuard, ProjectDatabase
from hearflow.services.fsutil import sha256_file

DATABASE_NAME = "project.sqlite3"
SUMMARY_NAME = "project.hearflow.json"
PROJECT_DIRECTORIES = (
    ".hearflow",
    ".hearflow/cache",
    ".hearflow/temp",
    ".hearflow/backups",
    "transcripts",
    "subtitles",
    "exports",
    "reports",
    "logs",
)


class ProjectRepository:
    """Expose project use-cases while SQLite remains the only source of truth."""

    def __init__(self, root: str | Path, database: ProjectDatabase | None = None) -> None:
        """Open a repository rooted at an existing project directory."""

        self.root = Path(root).expanduser().resolve(strict=False)
        self.database = database or ProjectDatabase(self.root / DATABASE_NAME)

    @classmethod
    def create(
        cls,
        root: str | Path,
        name: str,
        *,
        source_language: str = "auto",
        target_language: str | None = "zh-TW",
        translation_style: str = "台灣繁體中文",
    ) -> ProjectRepository:
        """Create a new project without overwriting an existing database."""

        selected_root = Path(root).expanduser().resolve(strict=False)
        selected_name = validate_project_name(name)
        database_path = selected_root / DATABASE_NAME
        if database_path.exists():
            raise FileExistsError(f"專案已存在：{database_path}")
        selected_root.mkdir(parents=True, exist_ok=True)
        for relative in PROJECT_DIRECTORIES:
            (selected_root / relative).mkdir(parents=True, exist_ok=True)

        repository = cls(selected_root)
        now = utc_now_iso()
        project_id = str(uuid.uuid4())
        with repository.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO projects(
                    id, name, root_path, source_language, target_language,
                    translation_style, settings_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?)
                """,
                (
                    project_id,
                    selected_name,
                    str(selected_root),
                    source_language or "auto",
                    target_language,
                    translation_style,
                    now,
                    now,
                ),
            )
        repository.export_summary()
        return repository

    @classmethod
    def open(cls, root: str | Path) -> ProjectRepository:
        """Open a project and reject folders without a canonical database."""

        selected_root = Path(root).expanduser().resolve(strict=False)
        database_path = selected_root / DATABASE_NAME
        if not database_path.is_file():
            raise FileNotFoundError(f"找不到 HearFlow 專案：{database_path}")
        repository = cls(selected_root)
        if repository.database.fetchone("SELECT id FROM projects LIMIT 1") is None:
            repository.close()
            raise ValueError("專案資料庫缺少 project 記錄")
        repository.recover_interrupted_jobs()
        return repository

    @property
    def project_id(self) -> str:
        """Return the sole project id stored in this project database."""

        row = self.database.fetchone("SELECT id FROM projects LIMIT 1")
        if row is None:
            raise RuntimeError("專案資料庫尚未初始化")
        return str(row["id"])

    def manifest(self) -> ProjectManifest:
        """Build the portable summary from canonical SQLite state."""

        row = self.database.fetchone("SELECT * FROM projects LIMIT 1")
        if row is None:
            raise RuntimeError("專案資料庫缺少 project 記錄")
        return ProjectManifest(
            id=str(row["id"]),
            name=str(row["name"]),
            root_path=self.root,
            source_language=str(row["source_language"]),
            target_language=(
                None if row["target_language"] is None else str(row["target_language"])
            ),
            translation_style=str(row["translation_style"]),
            settings=_json_object(row["settings_json"]),
            jobs=self.list_jobs(),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            schema_version=self.database.schema_version,
        )

    def update_project(
        self,
        *,
        name: str,
        source_language: str,
        target_language: str | None,
        translation_style: str,
        settings: dict[str, Any] | None = None,
    ) -> None:
        """Update project settings and immediately refresh the derived summary."""

        now = utc_now_iso()
        safe_settings = without_secrets(settings or {})
        self.database.execute(
            """
            UPDATE projects
               SET name = ?, source_language = ?, target_language = ?,
                   translation_style = ?, settings_json = ?, updated_at = ?
             WHERE id = ?
            """,
            (
                validate_project_name(name),
                source_language or "auto",
                target_language,
                translation_style,
                _json_dump(safe_settings),
                now,
                self.project_id,
            ),
        )
        self.export_summary()

    def add_media(
        self,
        path: str | Path,
        *,
        metadata: MediaMetadata | None = None,
        fingerprint: str | None = None,
    ) -> MediaJob:
        """Add one media path idempotently and return its canonical job."""

        source = Path(path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError("媒體來源必須是檔案")
        existing = self.database.fetchone(
            "SELECT * FROM media_jobs WHERE project_id = ? AND source_path = ?",
            (self.project_id, str(source)),
        )
        if existing is not None:
            existing_fingerprint = (
                None if existing["fingerprint"] is None else str(existing["fingerprint"])
            )
            current_size = source.stat().st_size
            content_changed = int(existing["size_bytes"]) != current_size or (
                fingerprint is not None and fingerprint != existing_fingerprint
            )
            if not content_changed:
                return self._job_from_row(existing)
            job_id = str(existing["id"])
            refreshed = False
            with self.database.transaction() as connection:
                latest = connection.execute(
                    "SELECT * FROM media_jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if latest is None:
                    raise KeyError(f"找不到媒體工作：{job_id}")
                latest_fingerprint = (
                    None if latest["fingerprint"] is None else str(latest["fingerprint"])
                )
                latest_changed = int(latest["size_bytes"]) != current_size or (
                    fingerprint is not None and fingerprint != latest_fingerprint
                )
                if not latest_changed:
                    return self._job_from_row(latest)
                old_generation = int(latest["generation_id"])
                new_generation = old_generation + 1
                attempt_id = int(latest["attempt_id"])
                now = utc_now_iso()
                message = f"來源內容已變更，舊字幕與產物記錄已失效：{source.name}"
                connection.execute(
                    """
                    UPDATE job_attempts
                       SET status = ?, error_message = ?, finished_at = ?
                     WHERE job_id = ? AND attempt_id = ?
                       AND generation_id = ? AND finished_at IS NULL
                    """,
                    (
                        JobStatus.FAILED.value,
                        message,
                        now,
                        job_id,
                        attempt_id,
                        old_generation,
                    ),
                )
                connection.execute(
                    "DELETE FROM subtitle_segments WHERE job_id = ?",
                    (job_id,),
                )
                connection.execute(
                    "DELETE FROM artifacts WHERE job_id = ?",
                    (job_id,),
                )
                connection.execute(
                    """
                    UPDATE media_jobs
                       SET fingerprint = ?, size_bytes = ?, status = ?,
                           translation_status = ?, metadata_json = NULL,
                           raw_response_json = NULL, response_headers_json = NULL,
                           error_message = NULL, generation_id = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        fingerprint,
                        current_size,
                        JobStatus.PENDING.value,
                        TranslationStatus.NOT_REQUESTED.value,
                        new_generation,
                        now,
                        job_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO event_log(
                        project_id, job_id, level, event_type, message,
                        attempt_id, generation_id, created_at
                    ) VALUES (?, ?, 'warning', 'media_content_changed', ?, ?, ?, ?)
                    """,
                    (
                        self.project_id,
                        job_id,
                        message,
                        attempt_id,
                        new_generation,
                        now,
                    ),
                )
                refreshed = True
            if not refreshed:
                return self.get_job(job_id)
            self.export_summary()
            return self.get_job(job_id)
        now = utc_now_iso()
        job = MediaJob(
            id=str(uuid.uuid4()),
            project_id=self.project_id,
            source_path=source,
            fingerprint=fingerprint,
            size_bytes=source.stat().st_size,
            metadata=metadata,
            created_at=now,
            updated_at=now,
        )
        self.database.execute(
            """
            INSERT INTO media_jobs(
                id, project_id, source_path, source_name, fingerprint, size_bytes,
                status, translation_status, metadata_json, attempt_id,
                generation_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
            """,
            (
                job.id,
                job.project_id,
                str(job.source_path),
                job.source_name,
                job.fingerprint,
                job.size_bytes,
                job.status.value,
                job.translation_status.value,
                None if metadata is None else _json_dump(metadata.to_dict()),
                now,
                now,
            ),
        )
        self.log_event("info", "media_added", f"已加入媒體：{job.source_name}", job_id=job.id)
        self.export_summary()
        return job

    def list_jobs(self) -> list[MediaJob]:
        """Return all media jobs in stable creation order."""

        rows = self.database.fetchall(
            "SELECT * FROM media_jobs WHERE project_id = ? ORDER BY created_at, source_name",
            (self.project_id,),
        )
        return [self._job_from_row(row) for row in rows]

    def get_job(self, job_id: str) -> MediaJob:
        """Return one media job or raise ``KeyError``."""

        row = self.database.fetchone("SELECT * FROM media_jobs WHERE id = ?", (job_id,))
        if row is None:
            raise KeyError(f"找不到媒體工作：{job_id}")
        return self._job_from_row(row)

    def set_job_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        error_message: str | None = None,
        translation_status: TranslationStatus | None = None,
    ) -> None:
        """Persist one honest job status transition."""

        if not isinstance(status, JobStatus):
            raise TypeError("status must be JobStatus")
        now = utc_now_iso()
        if translation_status is None:
            changed = self.database.execute(
                """
                UPDATE media_jobs
                   SET status = ?, error_message = ?, updated_at = ?
                 WHERE id = ?
                """,
                (status.value, error_message, now, job_id),
            )
        else:
            changed = self.database.execute(
                """
                UPDATE media_jobs
                   SET status = ?, translation_status = ?, error_message = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    status.value,
                    translation_status.value,
                    error_message,
                    now,
                    job_id,
                ),
            )
        if changed != 1:
            raise KeyError(f"找不到媒體工作：{job_id}")

    def set_job_status_if_current(
        self,
        job_id: str,
        status: JobStatus,
        *,
        attempt_id: int,
        generation_id: int,
        error_message: str | None = None,
        translation_status: TranslationStatus | None = None,
    ) -> bool:
        """Update status only while the supplied attempt tokens remain current."""

        now = utc_now_iso()
        assignments = "status = ?, error_message = ?, updated_at = ?"
        parameters: list[Any] = [status.value, error_message, now]
        if translation_status is not None:
            assignments += ", translation_status = ?"
            parameters.append(translation_status.value)
        parameters.extend((job_id, attempt_id, generation_id))
        changed = self.database.execute(
            f"""
            UPDATE media_jobs SET {assignments}
             WHERE id = ? AND attempt_id = ? AND generation_id = ?
            """,
            parameters,
        )
        return changed == 1

    def save_transcription(
        self,
        job_id: str,
        result: AsrResult,
        *,
        attempt_id: int,
        generation_id: int,
    ) -> bool:
        """Commit a validated ASR result only when its attempt is still current."""

        guard = AttemptGuard(self.database, job_id)
        now = utc_now_iso()

        def mutation(connection: sqlite3.Connection) -> None:
            connection.execute("DELETE FROM subtitle_segments WHERE job_id = ?", (job_id,))
            for ordinal, segment in enumerate(result.segments):
                connection.execute(
                    """
                    INSERT INTO subtitle_segments(
                        id, job_id, ordinal, start_ms, end_ms, source_text,
                        translated_text, status, metadata_json, attempt_id,
                        generation_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        segment.id,
                        job_id,
                        ordinal,
                        segment.start_ms,
                        segment.end_ms,
                        segment.source_text,
                        segment.translated_text,
                        segment.status,
                        _json_dump(segment.metadata),
                        attempt_id,
                        generation_id,
                        now,
                        now,
                    ),
                )
            connection.execute(
                """
                UPDATE media_jobs
                   SET status = ?, raw_response_json = ?, response_headers_json = ?,
                       error_message = NULL, updated_at = ?
                 WHERE id = ?
                """,
                (
                    JobStatus.CLEANING.value,
                    _json_dump(without_secrets(result.raw_response)),
                    _json_dump(without_secrets(result.response_headers)),
                    now,
                    job_id,
                ),
            )
            connection.execute(
                """
                UPDATE job_attempts SET status = ?, finished_at = ?
                 WHERE job_id = ? AND attempt_id = ? AND generation_id = ?
                """,
                (
                    JobStatus.CLEANING.value,
                    now,
                    job_id,
                    attempt_id,
                    generation_id,
                ),
            )

        committed = guard.commit_if_current(attempt_id, generation_id, mutation)
        if committed:
            self.log_event(
                "info",
                "transcription_saved",
                "ASR 轉錄已保存",
                job_id=job_id,
                attempt_id=attempt_id,
                generation_id=generation_id,
            )
        return committed

    def load_segments(self, job_id: str) -> list[SubtitleSegment]:
        """Load editable segments in ordinal order."""

        rows = self.database.fetchall(
            "SELECT * FROM subtitle_segments WHERE job_id = ? ORDER BY ordinal",
            (job_id,),
        )
        return [
            SubtitleSegment(
                id=str(row["id"]),
                start_ms=int(row["start_ms"]),
                end_ms=int(row["end_ms"]),
                source_text=str(row["source_text"]),
                translated_text=(
                    None if row["translated_text"] is None else str(row["translated_text"])
                ),
                status=str(row["status"]),
                metadata=_json_object(row["metadata_json"]),
            )
            for row in rows
        ]

    def replace_segments(self, job_id: str, segments: Iterable[SubtitleSegment]) -> None:
        """Replace the editable segment set in one transaction."""

        materialized = list(segments)
        now = utc_now_iso()
        job = self.get_job(job_id)
        with self.database.transaction() as connection:
            _replace_segments_in_connection(
                connection,
                job_id,
                materialized,
                attempt_id=job.attempt_id,
                generation_id=job.generation_id,
                now=now,
            )
            connection.execute(
                "UPDATE media_jobs SET updated_at = ? WHERE id = ?",
                (now, job_id),
            )

    def replace_segments_if_current(
        self,
        job_id: str,
        segments: Iterable[SubtitleSegment],
        *,
        attempt_id: int,
        generation_id: int,
    ) -> bool:
        """Atomically replace segments only for the still-current attempt."""

        materialized = list(segments)
        now = utc_now_iso()
        guard = AttemptGuard(self.database, job_id)

        def mutation(connection: sqlite3.Connection) -> None:
            _replace_segments_in_connection(
                connection,
                job_id,
                materialized,
                attempt_id=attempt_id,
                generation_id=generation_id,
                now=now,
            )
            connection.execute(
                "UPDATE media_jobs SET updated_at = ? WHERE id = ?",
                (now, job_id),
            )

        return guard.commit_if_current(attempt_id, generation_id, mutation)

    def add_artifact(
        self,
        job_id: str,
        kind: ArtifactKind,
        path: str | Path,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Path:
        """Register a real output file and its hash."""

        artifact_path = Path(path).expanduser().resolve(strict=True)
        if not artifact_path.is_file():
            raise ValueError("artifact path must identify a file")
        job = self.get_job(job_id)
        digest = sha256_file(artifact_path)
        self.database.execute(
            """
            INSERT OR REPLACE INTO artifacts(
                id, job_id, kind, path, sha256, size_bytes, attempt_id,
                generation_id, metadata_json, created_at
            ) VALUES (
                COALESCE(
                    (SELECT id FROM artifacts WHERE job_id = ? AND kind = ? AND path = ?),
                    ?
                ),
                ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                job_id,
                kind.value,
                str(artifact_path),
                str(uuid.uuid4()),
                job_id,
                kind.value,
                str(artifact_path),
                digest,
                artifact_path.stat().st_size,
                job.attempt_id,
                job.generation_id,
                _json_dump(without_secrets(metadata or {})),
                utc_now_iso(),
            ),
        )
        return artifact_path

    def add_artifact_if_current(
        self,
        job_id: str,
        kind: ArtifactKind,
        path: str | Path,
        *,
        attempt_id: int,
        generation_id: int,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Register an output only when it belongs to the current attempt."""

        artifact_path = Path(path).expanduser().resolve(strict=True)
        if not artifact_path.is_file():
            raise ValueError("artifact path must identify a file")
        digest = sha256_file(artifact_path)
        guard = AttemptGuard(self.database, job_id)

        def mutation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT OR REPLACE INTO artifacts(
                    id, job_id, kind, path, sha256, size_bytes, attempt_id,
                    generation_id, metadata_json, created_at
                ) VALUES (
                    COALESCE(
                        (SELECT id FROM artifacts
                          WHERE job_id = ? AND kind = ? AND path = ?),
                        ?
                    ),
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    job_id,
                    kind.value,
                    str(artifact_path),
                    str(uuid.uuid4()),
                    job_id,
                    kind.value,
                    str(artifact_path),
                    digest,
                    artifact_path.stat().st_size,
                    attempt_id,
                    generation_id,
                    _json_dump(without_secrets(metadata or {})),
                    utc_now_iso(),
                ),
            )

        return guard.commit_if_current(attempt_id, generation_id, mutation)

    def log_event(
        self,
        level: str,
        event_type: str,
        message: str,
        *,
        job_id: str | None = None,
        details: dict[str, Any] | None = None,
        attempt_id: int | None = None,
        generation_id: int | None = None,
    ) -> None:
        """Append a structured, secret-redacted event."""

        self.database.execute(
            """
            INSERT INTO event_log(
                project_id, job_id, level, event_type, message, details_json,
                attempt_id, generation_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.project_id,
                job_id,
                level,
                event_type,
                message,
                _json_dump(without_secrets(details or {})),
                attempt_id,
                generation_id,
                utc_now_iso(),
            ),
        )

    def list_events(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Return the newest structured events in display order."""

        rows = self.database.fetchall(
            """
            SELECT * FROM event_log WHERE project_id = ?
             ORDER BY id DESC LIMIT ?
            """,
            (self.project_id, max(1, min(limit, 5_000))),
        )
        return [
            {
                "id": int(row["id"]),
                "job_id": row["job_id"],
                "level": str(row["level"]),
                "event_type": str(row["event_type"]),
                "message": str(row["message"]),
                "details": _json_object(row["details_json"]),
                "created_at": str(row["created_at"]),
            }
            for row in reversed(rows)
        ]

    def recover_interrupted_jobs(self) -> int:
        """Mark crash-left active states as failed/retryable."""

        now = utc_now_iso()
        statuses = (
            JobStatus.INSPECTING.value,
            JobStatus.TRANSCRIBING.value,
            JobStatus.CLEANING.value,
            JobStatus.TRANSLATING.value,
            JobStatus.QA.value,
            JobStatus.EXPORTING.value,
            JobStatus.CANCELLING.value,
        )
        placeholders = ",".join("?" for _ in statuses)
        message = "上次執行中斷，可安全重試。"
        with self.database.transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT id, project_id, attempt_id, generation_id
                  FROM media_jobs
                 WHERE status IN ({placeholders})
                """,
                statuses,
            ).fetchall()
            for row in rows:
                old_generation = int(row["generation_id"])
                new_generation = old_generation + 1
                connection.execute(
                    """
                    UPDATE media_jobs
                       SET status = ?, generation_id = ?, error_message = ?,
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        JobStatus.FAILED.value,
                        new_generation,
                        message,
                        now,
                        str(row["id"]),
                    ),
                )
                connection.execute(
                    """
                    UPDATE job_attempts
                       SET status = ?, error_message = ?, finished_at = ?
                     WHERE job_id = ? AND attempt_id = ?
                       AND generation_id = ? AND finished_at IS NULL
                    """,
                    (
                        JobStatus.FAILED.value,
                        message,
                        now,
                        str(row["id"]),
                        int(row["attempt_id"]),
                        old_generation,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO event_log(
                        project_id, job_id, level, event_type, message,
                        attempt_id, generation_id, created_at
                    ) VALUES (?, ?, 'warning', 'interrupted_job_recovered', ?, ?, ?, ?)
                    """,
                    (
                        str(row["project_id"]),
                        str(row["id"]),
                        message,
                        int(row["attempt_id"]),
                        new_generation,
                        now,
                    ),
                )
        return len(rows)

    def export_summary(self) -> Path:
        """Atomically regenerate the derived, non-canonical project summary."""

        destination = self.root / SUMMARY_NAME
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        encoded = json.dumps(
            without_secrets(self.manifest().to_dict()),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        with temporary.open("wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        return destination

    def close(self) -> None:
        """Close the canonical database."""

        self.database.close()

    def _job_from_row(self, row: sqlite3.Row) -> MediaJob:
        metadata_payload = (
            None if row["metadata_json"] is None else _json_object(row["metadata_json"])
        )
        artifacts = self.database.fetchall(
            "SELECT kind, path FROM artifacts WHERE job_id = ? ORDER BY created_at",
            (str(row["id"]),),
        )
        artifact_map: dict[str, str] = {}
        for artifact in artifacts:
            key = str(artifact["kind"])
            path = str(artifact["path"])
            if key in artifact_map:
                key = f"{key}:{Path(path).name}"
            artifact_map[key] = path
        return MediaJob(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            source_path=Path(str(row["source_path"])),
            status=JobStatus(str(row["status"])),
            translation_status=TranslationStatus(str(row["translation_status"])),
            fingerprint=None if row["fingerprint"] is None else str(row["fingerprint"]),
            size_bytes=int(row["size_bytes"]),
            metadata=(
                None if metadata_payload is None else MediaMetadata.from_dict(metadata_payload)
            ),
            attempt_id=int(row["attempt_id"]),
            generation_id=int(row["generation_id"]),
            error_message=(None if row["error_message"] is None else str(row["error_message"])),
            artifacts=artifact_map,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        raise TypeError("JSON database field must be text")
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("JSON database field must contain an object")
    return parsed


def _replace_segments_in_connection(
    connection: sqlite3.Connection,
    job_id: str,
    segments: list[SubtitleSegment],
    *,
    attempt_id: int,
    generation_id: int,
    now: str,
) -> None:
    connection.execute("DELETE FROM subtitle_segments WHERE job_id = ?", (job_id,))
    for ordinal, segment in enumerate(segments):
        connection.execute(
            """
            INSERT INTO subtitle_segments(
                id, job_id, ordinal, start_ms, end_ms, source_text,
                translated_text, status, metadata_json, attempt_id,
                generation_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                segment.id,
                job_id,
                ordinal,
                segment.start_ms,
                segment.end_ms,
                segment.source_text,
                segment.translated_text,
                segment.status,
                _json_dump(segment.metadata),
                attempt_id,
                generation_id,
                now,
                now,
            ),
        )
