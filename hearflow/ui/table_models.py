"""Qt table models for media jobs and editable subtitle segments."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor

from hearflow.domain.models import JobStatus, MediaJob, SubtitleSegment

_STATUS_LABELS = {
    JobStatus.PENDING: "等待中",
    JobStatus.INSPECTING: "檢查媒體",
    JobStatus.READY: "準備完成",
    JobStatus.TRANSCRIBING: "語音辨識中",
    JobStatus.CLEANING: "整理文字",
    JobStatus.TRANSLATING: "翻譯中",
    JobStatus.QA: "品質檢查",
    JobStatus.EXPORTING: "輸出中",
    JobStatus.COMPLETED: "已完成",
    JobStatus.CANCELLING: "正在取消",
    JobStatus.CANCELLED: "已取消",
    JobStatus.FAILED: "失敗",
}
_INVALID_INDEX = QModelIndex()
_ModelIndex = QModelIndex | QPersistentModelIndex


def format_time(milliseconds: int) -> str:
    """Format milliseconds as a friendly timeline value."""

    value = max(0, milliseconds)
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def parse_time(value: str) -> int:
    """Parse ``HH:MM:SS.mmm`` or a raw millisecond number."""

    text = value.strip().replace(",", ".")
    if ":" not in text:
        return int(text)
    parts = text.split(":")
    if len(parts) != 3:
        raise ValueError("時間格式必須是 HH:MM:SS.mmm")
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = float(parts[2])
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        raise ValueError("時間超出範圍")
    return round((hours * 3_600 + minutes * 60 + seconds) * 1_000)


class JobTableModel(QAbstractTableModel):
    """Read-only queue presentation."""

    headers = ("檔案", "狀態", "翻譯", "長度", "更新時間", "錯誤")

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._jobs: list[MediaJob] = []

    @property
    def jobs(self) -> tuple[MediaJob, ...]:
        return tuple(self._jobs)

    def set_jobs(self, jobs: Sequence[MediaJob]) -> None:
        self.beginResetModel()
        self._jobs = list(jobs)
        self.endResetModel()

    def job_at(self, row: int) -> MediaJob | None:
        return self._jobs[row] if 0 <= row < len(self._jobs) else None

    def rowCount(self, parent: _ModelIndex = _INVALID_INDEX) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._jobs)

    def columnCount(self, parent: _ModelIndex = _INVALID_INDEX) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.headers)

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if (
            orientation is Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(self.headers)
        ):
            return self.headers[section]
        return super().headerData(section, orientation, role)

    def data(self, index: _ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._jobs):
            return None
        job = self._jobs[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            duration = "—"
            if job.metadata is not None:
                duration = format_time(round(job.metadata.duration_seconds * 1_000))
            values = (
                job.source_name,
                _STATUS_LABELS.get(job.status, job.status.value),
                job.translation_status.value,
                duration,
                job.updated_at.replace("T", " ")[:19],
                job.error_message or "",
            )
            return values[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(job.source_path)
        if role == Qt.ItemDataRole.ForegroundRole and job.status is JobStatus.FAILED:
            return QColor("#b42318")
        if role == Qt.ItemDataRole.UserRole:
            return job.id
        return None


class SegmentTableModel(QAbstractTableModel):
    """Editable subtitle document with validation-aware cells."""

    changed = Signal()
    validation_error = Signal(str)
    headers = ("#", "開始", "結束", "原文", "翻譯", "狀態")

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._segments: list[SubtitleSegment] = []

    @property
    def segments(self) -> list[SubtitleSegment]:
        return [item.clone() for item in self._segments]

    def set_segments(self, segments: Sequence[SubtitleSegment]) -> None:
        self.beginResetModel()
        self._segments = [item.clone() for item in segments]
        self.endResetModel()

    def mutate(self, operation: Callable[[list[SubtitleSegment]], None]) -> None:
        self.beginResetModel()
        operation(self._segments)
        self.endResetModel()
        self.changed.emit()

    def segment_at(self, row: int) -> SubtitleSegment | None:
        return self._segments[row] if 0 <= row < len(self._segments) else None

    def rowCount(self, parent: _ModelIndex = _INVALID_INDEX) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._segments)

    def columnCount(self, parent: _ModelIndex = _INVALID_INDEX) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.headers)

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if (
            orientation is Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(self.headers)
        ):
            return self.headers[section]
        return super().headerData(section, orientation, role)

    def flags(self, index: _ModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if index.isValid() and index.column() in {1, 2, 3, 4}:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def data(self, index: _ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._segments):
            return None
        item = self._segments[index.row()]
        if role in {Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole}:
            values = (
                index.row() + 1,
                format_time(item.start_ms),
                format_time(item.end_ms),
                item.source_text,
                item.translated_text or "",
                item.status,
            )
            return values[index.column()]
        if role == Qt.ItemDataRole.BackgroundRole and item.end_ms <= item.start_ms:
            return QColor("#fff1f0")
        if role == Qt.ItemDataRole.ToolTipRole and item.end_ms <= item.start_ms:
            return "結束時間必須晚於開始時間"
        return None

    def setData(  # noqa: N802
        self,
        index: _ModelIndex,
        value: Any,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if (
            role != Qt.ItemDataRole.EditRole
            or not index.isValid()
            or not 0 <= index.row() < len(self._segments)
        ):
            return False
        item = self._segments[index.row()]
        try:
            if index.column() == 1:
                item.start_ms = parse_time(str(value))
            elif index.column() == 2:
                item.end_ms = parse_time(str(value))
            elif index.column() == 3:
                item.source_text = str(value)
            elif index.column() == 4:
                text = str(value)
                item.translated_text = text if text else None
            else:
                return False
        except (TypeError, ValueError) as exc:
            self.validation_error.emit(str(exc))
            return False
        self.dataChanged.emit(index, index)
        self.changed.emit()
        return True
