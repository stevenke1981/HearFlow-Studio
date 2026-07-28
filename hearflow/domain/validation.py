"""Reusable validation and secret-safety helpers."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .models import JsonValue, SubtitleSegment


class ValidationError(ValueError):
    """Raised when user or external-service data violates a public contract."""


_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)


def validate_project_name(name: str) -> str:
    """Validate and return a trimmed Windows-compatible project name."""

    if not isinstance(name, str):
        raise ValidationError("專案名稱必須是文字")
    cleaned = name.strip()
    if not cleaned:
        raise ValidationError("專案名稱不可為空白")
    if cleaned.endswith((".", " ")):
        raise ValidationError("專案名稱不可用句點或空白結尾")
    if _INVALID_WINDOWS_CHARS.search(cleaned):
        raise ValidationError("專案名稱含有 Windows 不允許的字元")
    if cleaned.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise ValidationError("專案名稱是 Windows 保留名稱")
    if len(cleaned) > 120:
        raise ValidationError("專案名稱不可超過 120 個字元")
    return cleaned


def validate_temperature(value: float) -> float:
    """Return a finite ASR temperature in the upstream-supported range."""

    try:
        temperature = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("temperature 必須是數字") from exc
    if not math.isfinite(temperature) or not 0.0 <= temperature <= 1.0:
        raise ValidationError("temperature 必須介於 0.0 與 1.0")
    return temperature


def validate_prompt(prompt: str, *, max_utf8_bytes: int = 4096) -> str:
    """Validate the experimental ASR prompt by UTF-8 byte length."""

    if not isinstance(prompt, str):
        raise ValidationError("prompt 必須是文字")
    if len(prompt.encode("utf-8")) > max_utf8_bytes:
        raise ValidationError(f"prompt 的 UTF-8 長度不可超過 {max_utf8_bytes} bytes")
    return prompt


def validate_media_path(
    path: str | Path,
    *,
    allowed_extensions: set[str] | frozenset[str],
) -> Path:
    """Return an absolute readable media path after cheap local checks."""

    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValidationError(f"找不到媒體檔案：{candidate}") from exc
    if not resolved.is_file():
        raise ValidationError(f"媒體路徑不是檔案：{resolved}")
    if resolved.suffix.casefold() not in {item.casefold() for item in allowed_extensions}:
        raise ValidationError(f"不支援的媒體格式：{resolved.suffix or '(無副檔名)'}")
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise ValidationError(f"無法讀取媒體檔案：{resolved}") from exc
    if size <= 0:
        raise ValidationError(f"媒體檔案是空的：{resolved}")
    return resolved


def validate_positive_duration(value: Any, *, field_name: str = "duration") -> float:
    """Return a finite positive duration parsed from FFprobe data."""

    try:
        duration = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} 不是有效數字") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ValidationError(f"{field_name} 必須大於 0")
    return duration


def validate_segments_for_export(
    segments: Sequence[SubtitleSegment],
    *,
    require_translation: bool = False,
) -> None:
    """Reject conditions that would produce structurally invalid timed output."""

    if not segments:
        raise ValidationError("沒有可匯出的字幕片段")
    previous_start = -1
    previous_end = -1
    seen_ids: set[str] = set()
    for segment in segments:
        if segment.id in seen_ids:
            raise ValidationError(f"字幕片段 id 重複：{segment.id}")
        seen_ids.add(segment.id)
        if segment.start_ms < 0 or segment.end_ms <= segment.start_ms:
            raise ValidationError(f"字幕片段 {segment.id} 的時間範圍無效")
        if segment.start_ms < previous_start:
            raise ValidationError(f"字幕片段 {segment.id} 的開始時間倒退")
        if segment.start_ms < previous_end:
            raise ValidationError(f"字幕片段 {segment.id} 與前一片段重疊")
        if not segment.source_text.strip():
            raise ValidationError(f"字幕片段 {segment.id} 的原文是空白")
        if require_translation and (
            segment.translated_text is None or not segment.translated_text.strip()
        ):
            raise ValidationError(f"字幕片段 {segment.id} 缺少翻譯")
        previous_start = segment.start_ms
        previous_end = segment.end_ms


def is_secret_key(key: object) -> bool:
    """Return whether a mapping key commonly identifies credential material."""

    normalised = str(key).strip().casefold().replace("-", "_").replace(" ", "_")
    return any(part in normalised for part in _SECRET_KEY_PARTS)


def without_secrets(value: Any) -> JsonValue:
    """Return JSON-safe data with credential fields removed.

    This helper is intended for canonical project settings and portable manifests.
    Reports use a visible ``[REDACTED]`` marker instead, implemented in
    :mod:`hearflow.services.reporting`.
    """

    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if is_secret_key(key):
                continue
            result[str(key)] = without_secrets(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [without_secrets(item) for item in value]
    return str(value)


def ensure_output_is_not_source(source: str | Path, output: str | Path) -> None:
    """Reject in-place media writes using case-insensitive resolved paths."""

    source_path = Path(source).expanduser().resolve(strict=False)
    output_path = Path(output).expanduser().resolve(strict=False)
    if str(source_path).casefold() == str(output_path).casefold():
        raise ValidationError("輸出檔案不可覆蓋原始媒體")
