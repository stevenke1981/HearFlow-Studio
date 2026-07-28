"""Typed domain models used throughout HearFlow Studio.

The classes in this module deliberately contain no UI or persistence logic.  They
form a small, serialisable boundary between the SQLite repository, background
workers, the ASR gateway, and the Qt presentation layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


def utc_now_iso() -> str:
    """Return a stable, timezone-aware UTC timestamp for persisted records."""

    return datetime.now(UTC).isoformat(timespec="milliseconds")


class JobStatus(StrEnum):
    """Canonical lifecycle states for one media job."""

    PENDING = "pending"
    INSPECTING = "inspecting"
    READY = "ready"
    TRANSCRIBING = "transcribing"
    CLEANING = "cleaning"
    TRANSLATING = "translating"
    QA = "qa"
    EXPORTING = "exporting"
    COMPLETED = "completed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"


class TranslationStatus(StrEnum):
    """Translation state kept separate from the overall media job state."""

    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


class ArtifactKind(StrEnum):
    """Kinds of files produced without modifying the original media."""

    RAW_RESPONSE = "raw_response"
    TRANSCRIPT = "transcript"
    SUBTITLE = "subtitle"
    EXPORT = "export"
    BURNED_VIDEO = "burned_video"
    REPORT = "report"
    LOG = "log"


class QaSeverity(StrEnum):
    """Severity used by subtitle quality-assurance findings."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKING = "blocking"


@dataclass(slots=True)
class SubtitleSegment:
    """One editable, millisecond-based subtitle segment.

    Invalid timing is intentionally allowed at construction time so imported raw
    ASR material is never silently discarded.  :class:`SubtitleQaService`
    reports those problems before export.
    """

    id: str
    start_ms: int
    end_ms: int
    source_text: str
    translated_text: str | None = None
    status: str = "raw"
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalise primitive values while preserving timing defects for QA."""

        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("segment id must be a non-empty string")
        if isinstance(self.start_ms, bool) or not isinstance(self.start_ms, int):
            raise TypeError("start_ms must be an integer")
        if isinstance(self.end_ms, bool) or not isinstance(self.end_ms, int):
            raise TypeError("end_ms must be an integer")
        if not isinstance(self.source_text, str):
            raise TypeError("source_text must be a string")
        if self.translated_text is not None and not isinstance(self.translated_text, str):
            raise TypeError("translated_text must be a string or None")

    @property
    def duration_ms(self) -> int:
        """Return the signed segment duration in milliseconds."""

        return self.end_ms - self.start_ms

    @property
    def target_text(self) -> str | None:
        """Compatibility alias used by translation-oriented callers."""

        return self.translated_text

    @target_text.setter
    def target_text(self, value: str | None) -> None:
        self.translated_text = value

    def display_text(self, *, prefer_translation: bool = False) -> str:
        """Return translated text when requested and actually available."""

        if prefer_translation and self.translated_text is not None:
            return self.translated_text
        return self.source_text

    def clone(self, **changes: Any) -> SubtitleSegment:
        """Return an independent copy, including an independent metadata mapping."""

        copied = replace(self, metadata=dict(self.metadata))
        return replace(copied, **changes)

    def to_dict(self) -> dict[str, JsonValue]:
        """Return the canonical JSON-compatible segment representation."""

        return {
            "id": self.id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "source_text": self.source_text,
            "translated_text": self.translated_text,
            "status": self.status,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SubtitleSegment:
        """Create a segment from SQLite/JSON data."""

        return cls(
            id=str(value["id"]),
            start_ms=int(value["start_ms"]),
            end_ms=int(value["end_ms"]),
            source_text=str(value.get("source_text", "")),
            translated_text=(
                None if value.get("translated_text") is None else str(value["translated_text"])
            ),
            status=str(value.get("status", "raw")),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class MediaMetadata:
    """FFprobe-derived metadata for a readable media file."""

    path: Path
    duration_seconds: float
    format_name: str
    audio_streams: tuple[dict[str, JsonValue], ...]
    video_streams: tuple[dict[str, JsonValue], ...] = ()
    size_bytes: int = 0
    bit_rate: int | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        """Return a portable JSON-compatible representation."""

        return {
            "path": str(self.path),
            "duration_seconds": self.duration_seconds,
            "format_name": self.format_name,
            "audio_streams": [dict(stream) for stream in self.audio_streams],
            "video_streams": [dict(stream) for stream in self.video_streams],
            "size_bytes": self.size_bytes,
            "bit_rate": self.bit_rate,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MediaMetadata:
        """Restore metadata from persisted JSON."""

        return cls(
            path=Path(str(value["path"])),
            duration_seconds=float(value["duration_seconds"]),
            format_name=str(value.get("format_name", "")),
            audio_streams=tuple(dict(item) for item in value.get("audio_streams", [])),
            video_streams=tuple(dict(item) for item in value.get("video_streams", [])),
            size_bytes=int(value.get("size_bytes", 0)),
            bit_rate=(None if value.get("bit_rate") is None else int(value["bit_rate"])),
        )


@dataclass(slots=True)
class MediaJob:
    """Canonical application view of one queued media item."""

    id: str
    project_id: str
    source_path: Path
    status: JobStatus = JobStatus.PENDING
    translation_status: TranslationStatus = TranslationStatus.NOT_REQUESTED
    fingerprint: str | None = None
    size_bytes: int = 0
    metadata: MediaMetadata | None = None
    attempt_id: int = 0
    generation_id: int = 0
    error_message: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    @property
    def source_name(self) -> str:
        """Return the source filename for display."""

        return self.source_path.name

    def to_dict(self) -> dict[str, JsonValue]:
        """Return a non-secret, JSON-compatible job summary."""

        return {
            "id": self.id,
            "project_id": self.project_id,
            "source_path": str(self.source_path),
            "status": self.status.value,
            "translation_status": self.translation_status.value,
            "fingerprint": self.fingerprint,
            "size_bytes": self.size_bytes,
            "metadata": None if self.metadata is None else self.metadata.to_dict(),
            "attempt_id": self.attempt_id,
            "generation_id": self.generation_id,
            "error_message": self.error_message,
            "artifacts": dict(self.artifacts),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class ProjectManifest:
    """Portable project summary generated from canonical SQLite state."""

    id: str
    name: str
    root_path: Path
    source_language: str = "auto"
    target_language: str | None = "zh-TW"
    translation_style: str = "台灣繁體中文"
    settings: dict[str, JsonValue] = field(default_factory=dict)
    jobs: list[MediaJob] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    schema_version: int = 1

    def to_dict(self) -> dict[str, JsonValue]:
        """Return the manifest representation written to ``project.json``."""

        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "root_path": str(self.root_path),
            "source_language": self.source_language,
            "target_language": self.target_language,
            "translation_style": self.translation_style,
            "settings": dict(self.settings),
            "jobs": [job.to_dict() for job in self.jobs],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class EngineStatus:
    """Combined liveness, readiness, and model state from the ASR gateway."""

    live: bool
    ready: bool
    models: tuple[str, ...] = ()
    detail: str = ""
    http_status: int | None = None


@dataclass(slots=True)
class AsrResult:
    """Validated verbose response returned by the Qwen3-ASR gateway."""

    raw_response: dict[str, Any]
    response_headers: dict[str, str]
    text: str
    language: str
    duration: float
    segments: list[SubtitleSegment]
    model: str | None = None
    chunk_count: int = 1
    timestamp_accuracy: str = "chunk"
    elapsed_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class QaIssue:
    """One actionable subtitle QA finding."""

    code: str
    severity: QaSeverity
    message: str
    segment_id: str | None = None
    details: dict[str, JsonValue] = field(default_factory=dict)

    @property
    def blocks_export(self) -> bool:
        """Return whether the finding must block an export."""

        return self.severity is QaSeverity.BLOCKING

    def to_dict(self) -> dict[str, JsonValue]:
        """Return a JSON-compatible QA record."""

        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "segment_id": self.segment_id,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class CleanRules:
    """Mechanical subtitle cleanup options."""

    unicode_nfkc: bool = True
    collapse_spaces: bool = True
    normalize_cjk_punctuation: bool = True
    trim_lines: bool = True
    remove_empty_segments: bool = False


@dataclass(frozen=True, slots=True)
class QaRules:
    """Thresholds used by :class:`SubtitleQaService`."""

    max_chars_per_line: int = 42
    max_cps: float = 20.0
    require_translation: bool = False
    min_duration_ms: int = 1


@dataclass(frozen=True, slots=True)
class AssStyle:
    """ASS rendering style expressed with user-friendly RGB colours."""

    font_name: str = "Microsoft JhengHei"
    font_size: int = 42
    primary_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    shadow: int = 1
    outline: int = 2
    margin_vertical: int = 36
    alignment: int = 2


@dataclass(frozen=True, slots=True)
class ExportOptions:
    """Options for one subtitle document export."""

    output_path: Path
    prefer_translation: bool = False
    require_translation: bool = False
    version_existing: bool = True
    language: str = "und"
    timestamp_accuracy: str = "chunk"
    ass_style: AssStyle = field(default_factory=AssStyle)
