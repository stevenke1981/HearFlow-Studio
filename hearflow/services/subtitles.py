"""Subtitle cleanup, editing, quality assurance, import, and export."""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path

from hearflow.domain.models import (
    AssStyle,
    CleanRules,
    ExportOptions,
    QaIssue,
    QaRules,
    QaSeverity,
    SubtitleSegment,
)
from hearflow.domain.validation import ValidationError, validate_segments_for_export
from hearflow.services.fsutil import atomic_write_text

_SPACE_RE = re.compile(r"[ \t\u00a0\u3000]+")
_SRT_BLOCK_RE = re.compile(
    r"(?ms)^\s*(?:\d+\s*\n)?"
    r"(?P<start>\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>\d{2}:\d{2}:\d{2}[,.]\d{3})[^\n]*\n"
    r"(?P<text>.*?)(?=\n{2,}|\Z)"
)
_CJK_PUNCTUATION = str.maketrans(
    {
        ",": "，",
        ":": "：",
        ";": "；",
        "?": "？",
        "!": "！",
    }
)


class SubtitleError(RuntimeError):
    """Raised when subtitle content cannot be safely processed."""


class SubtitleDocumentService:
    """Perform deterministic subtitle transformations and exports."""

    def clean(
        self,
        segments: Sequence[SubtitleSegment],
        rules: CleanRules | None = None,
    ) -> list[SubtitleSegment]:
        """Return cleaned copies while preserving IDs, timing, and raw metadata."""

        rules = rules or CleanRules()
        result: list[SubtitleSegment] = []
        for segment in segments:
            source = _clean_text(segment.source_text, rules)
            translated = (
                None
                if segment.translated_text is None
                else _clean_text(segment.translated_text, rules)
            )
            if rules.remove_empty_segments and not source and not translated:
                continue
            metadata = dict(segment.metadata)
            metadata["cleaned"] = True
            result.append(
                segment.clone(
                    source_text=source,
                    translated_text=translated,
                    status="cleaned",
                    metadata=metadata,
                )
            )
        return result

    def split(
        self,
        segments: Sequence[SubtitleSegment],
        segment_id: str,
        *,
        position: int,
        translated_position: int | None = None,
    ) -> list[SubtitleSegment]:
        """Split one segment and divide its time range proportionally."""

        output: list[SubtitleSegment] = []
        found = False
        for segment in segments:
            if segment.id != segment_id:
                output.append(segment.clone())
                continue
            found = True
            if segment.duration_ms < 2:
                raise ValueError("字幕片段太短，無法安全切割時間")
            if not 0 < position < len(segment.source_text):
                raise ValueError("切割位置必須位於原文內容中間")
            left_source = segment.source_text[:position].rstrip()
            right_source = segment.source_text[position:].lstrip()
            if not left_source or not right_source:
                raise ValueError("切割後兩側都必須有文字")
            translated = segment.translated_text
            if translated is None:
                left_translated = right_translated = None
            else:
                target_position = (
                    translated_position
                    if translated_position is not None
                    else round(len(translated) * position / len(segment.source_text))
                )
                if not 0 < target_position < len(translated):
                    left_translated = translated
                    right_translated = ""
                else:
                    left_translated = translated[:target_position].rstrip()
                    right_translated = translated[target_position:].lstrip()
            ratio = position / len(segment.source_text)
            split_ms = segment.start_ms + round(segment.duration_ms * ratio)
            split_ms = max(segment.start_ms + 1, min(split_ms, segment.end_ms - 1))
            metadata = dict(segment.metadata)
            metadata["edited"] = "split"
            output.extend(
                (
                    segment.clone(
                        source_text=left_source,
                        translated_text=left_translated,
                        end_ms=split_ms,
                        status="edited",
                        metadata=metadata,
                    ),
                    SubtitleSegment(
                        id=str(uuid.uuid4()),
                        start_ms=split_ms,
                        end_ms=segment.end_ms,
                        source_text=right_source,
                        translated_text=right_translated,
                        status="edited",
                        metadata=metadata,
                    ),
                )
            )
        if not found:
            raise KeyError(f"找不到字幕片段：{segment_id}")
        return output

    def merge(
        self,
        segments: Sequence[SubtitleSegment],
        first_id: str,
        second_id: str,
    ) -> list[SubtitleSegment]:
        """Merge two adjacent segments without inventing timing."""

        copied = [segment.clone() for segment in segments]
        first_index = next(
            (index for index, item in enumerate(copied) if item.id == first_id),
            None,
        )
        second_index = next(
            (index for index, item in enumerate(copied) if item.id == second_id),
            None,
        )
        if first_index is None or second_index is None:
            raise KeyError("找不到要合併的字幕片段")
        if second_index != first_index + 1:
            raise ValueError("只能合併相鄰字幕片段")
        first, second = copied[first_index], copied[second_index]
        translated = _join_optional(first.translated_text, second.translated_text)
        metadata = dict(first.metadata)
        metadata["merged_from"] = [first.id, second.id]
        copied[first_index : second_index + 1] = [
            first.clone(
                end_ms=max(first.end_ms, second.end_ms),
                source_text=_join_text(first.source_text, second.source_text),
                translated_text=translated,
                status="edited",
                metadata=metadata,
            )
        ]
        return copied

    def search_replace(
        self,
        segments: Sequence[SubtitleSegment],
        search: str,
        replacement: str,
        *,
        include_translation: bool = True,
        case_sensitive: bool = True,
    ) -> tuple[list[SubtitleSegment], int]:
        """Replace literal text and return independent segments plus match count."""

        if not search:
            raise ValueError("搜尋文字不得為空")
        pattern = re.compile(
            re.escape(search),
            0 if case_sensitive else re.IGNORECASE,
        )
        count = 0
        output: list[SubtitleSegment] = []
        for item in segments:
            source, source_count = pattern.subn(
                lambda _match: replacement,
                item.source_text,
            )
            target = item.translated_text
            target_count = 0
            if include_translation and target is not None:
                target, target_count = pattern.subn(
                    lambda _match: replacement,
                    target,
                )
            changed = source_count + target_count
            count += changed
            output.append(
                item.clone(
                    source_text=source,
                    translated_text=target,
                    status="edited" if changed else item.status,
                )
            )
        return output, count

    def export(
        self,
        segments: Sequence[SubtitleSegment],
        options: ExportOptions,
    ) -> Path:
        """Write a UTF-8 document atomically and never overwrite by default."""

        destination = _versioned_path(options.output_path, options.version_existing)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if options.require_translation:
            missing = [
                item.id
                for item in segments
                if item.translated_text is None or not item.translated_text.strip()
            ]
            if missing:
                raise SubtitleError(f"要求譯文輸出，但 {len(missing)} 個片段尚未翻譯。")
        try:
            validate_segments_for_export(
                segments,
                require_translation=(options.require_translation or options.prefer_translation),
            )
        except ValidationError as exc:
            raise SubtitleError(str(exc)) from exc
        suffix = destination.suffix.casefold()
        renderers = {
            ".txt": self._render_txt,
            ".json": self._render_json,
            ".srt": self._render_srt,
            ".vtt": self._render_vtt,
            ".ass": self._render_ass,
        }
        renderer = renderers.get(suffix)
        if renderer is None:
            raise SubtitleError(f"不支援的字幕輸出格式：{suffix or '無副檔名'}")
        if suffix in {".srt", ".vtt"}:
            for item in segments:
                text = item.display_text(prefer_translation=options.prefer_translation)
                if any(not line.strip() for line in text.splitlines()):
                    raise SubtitleError(
                        f"字幕片段 {item.id} 含空白行，會破壞 {suffix[1:].upper()} 結構。"
                    )
        content = renderer(segments, options)
        atomic_write_text(destination, content)
        return destination

    def import_srt(self, path: str | Path) -> list[SubtitleSegment]:
        """Import common SRT timing and text into editable segments."""

        source = Path(path).expanduser().resolve(strict=True)
        text = source.read_text(encoding="utf-8-sig")
        segments: list[SubtitleSegment] = []
        for index, match in enumerate(_SRT_BLOCK_RE.finditer(text)):
            segments.append(
                SubtitleSegment(
                    id=str(index),
                    start_ms=_parse_timestamp(match.group("start")),
                    end_ms=_parse_timestamp(match.group("end")),
                    source_text=match.group("text").strip(),
                    status="imported",
                    metadata={"timestamp_accuracy": "imported"},
                )
            )
        if not segments:
            raise SubtitleError("SRT 未包含可辨識的字幕片段。")
        return segments

    @staticmethod
    def _render_txt(
        segments: Sequence[SubtitleSegment],
        options: ExportOptions,
    ) -> str:
        return (
            "\n".join(
                item.display_text(prefer_translation=options.prefer_translation)
                for item in segments
            ).rstrip()
            + "\n"
        )

    @staticmethod
    def _render_json(
        segments: Sequence[SubtitleSegment],
        options: ExportOptions,
    ) -> str:
        payload = {
            "schema": "hearflow.subtitle.v1",
            "language": options.language,
            "timestamp_accuracy": options.timestamp_accuracy,
            "prefer_translation": options.prefer_translation,
            "segments": [item.to_dict() for item in segments],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    @staticmethod
    def _render_srt(
        segments: Sequence[SubtitleSegment],
        options: ExportOptions,
    ) -> str:
        blocks = []
        for index, item in enumerate(segments, 1):
            text = item.display_text(prefer_translation=options.prefer_translation)
            blocks.append(
                f"{index}\n{_srt_timestamp(item.start_ms)} --> "
                f"{_srt_timestamp(item.end_ms)}\n{text.strip()}"
            )
        return "\n\n".join(blocks).rstrip() + "\n"

    @staticmethod
    def _render_vtt(
        segments: Sequence[SubtitleSegment],
        options: ExportOptions,
    ) -> str:
        blocks = [
            "WEBVTT",
            "",
            f"NOTE HearFlow timestamp_accuracy={options.timestamp_accuracy}",
            "",
        ]
        for item in segments:
            text = item.display_text(prefer_translation=options.prefer_translation)
            blocks.extend(
                [
                    f"{_vtt_timestamp(item.start_ms)} --> {_vtt_timestamp(item.end_ms)}",
                    text.strip(),
                    "",
                ]
            )
        return "\n".join(blocks).rstrip() + "\n"

    @staticmethod
    def _render_ass(
        segments: Sequence[SubtitleSegment],
        options: ExportOptions,
    ) -> str:
        style = options.ass_style
        header = _ass_header(style)
        events = []
        for item in segments:
            text = item.display_text(prefer_translation=options.prefer_translation)
            escaped = (
                text.replace("\\", r"\\")
                .replace("{", r"\{")
                .replace("}", r"\}")
                .replace("\r\n", r"\N")
                .replace("\n", r"\N")
            )
            events.append(
                "Dialogue: 0,"
                f"{_ass_timestamp(item.start_ms)},{_ass_timestamp(item.end_ms)},"
                f"Default,,0,0,0,,{escaped}"
            )
        return header + "\n".join(events) + "\n"


class SubtitleQaService:
    """Inspect subtitle timing, readability, order, and translation coverage."""

    def inspect(
        self,
        segments: Sequence[SubtitleSegment],
        rules: QaRules | None = None,
    ) -> list[QaIssue]:
        rules = rules or QaRules()
        issues: list[QaIssue] = []
        previous_end: int | None = None
        ids: set[str] = set()
        for item in segments:
            if item.id in ids:
                issues.append(
                    QaIssue(
                        "duplicate_id",
                        QaSeverity.BLOCKING,
                        "字幕片段 ID 重複，無法可靠保存。",
                        item.id,
                    )
                )
            ids.add(item.id)
            if item.start_ms < 0 or item.end_ms <= item.start_ms:
                issues.append(
                    QaIssue(
                        "invalid_timing",
                        QaSeverity.BLOCKING,
                        "字幕起訖時間無效。",
                        item.id,
                    )
                )
            elif item.duration_ms < rules.min_duration_ms:
                issues.append(
                    QaIssue(
                        "too_short",
                        QaSeverity.ERROR,
                        f"字幕短於 {rules.min_duration_ms} ms。",
                        item.id,
                    )
                )
            if previous_end is not None and item.start_ms < previous_end:
                issues.append(
                    QaIssue(
                        "overlap",
                        QaSeverity.ERROR,
                        "字幕時間與前一片段重疊。",
                        item.id,
                    )
                )
            previous_end = item.end_ms
            if not item.source_text.strip():
                issues.append(
                    QaIssue(
                        "empty_source",
                        QaSeverity.ERROR,
                        "原文為空白。",
                        item.id,
                    )
                )
            selected_text = (
                item.translated_text if item.translated_text is not None else item.source_text
            )
            for line_index, line in enumerate(selected_text.splitlines() or [""]):
                if len(line) > rules.max_chars_per_line:
                    issues.append(
                        QaIssue(
                            "long_line",
                            QaSeverity.WARNING,
                            f"第 {line_index + 1} 行超過 {rules.max_chars_per_line} 字元。",
                            item.id,
                            {"characters": len(line)},
                        )
                    )
            if item.duration_ms > 0:
                cps = len(selected_text.replace("\n", "")) / (item.duration_ms / 1000)
                if cps > rules.max_cps:
                    issues.append(
                        QaIssue(
                            "high_cps",
                            QaSeverity.WARNING,
                            f"閱讀速度 {cps:.1f} CPS 超過 {rules.max_cps:.1f}。",
                            item.id,
                            {"cps": round(cps, 2)},
                        )
                    )
            if rules.require_translation and (
                item.translated_text is None or not item.translated_text.strip()
            ):
                issues.append(
                    QaIssue(
                        "missing_translation",
                        QaSeverity.ERROR,
                        "要求翻譯，但此片段沒有譯文。",
                        item.id,
                    )
                )
            if item.metadata.get("timestamp_accuracy") == "chunk":
                issues.append(
                    QaIssue(
                        "chunk_timestamp",
                        QaSeverity.INFO,
                        "此時間為 ASR 分片級近似值，可在時間軸手動微調。",
                        item.id,
                    )
                )
        if not segments:
            issues.append(
                QaIssue(
                    "no_segments",
                    QaSeverity.BLOCKING,
                    "沒有可匯出的字幕片段。",
                )
            )
        return issues


def _clean_text(text: str, rules: CleanRules) -> str:
    value = unicodedata.normalize("NFKC", text) if rules.unicode_nfkc else text
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    output: list[str] = []
    for line in lines:
        if rules.collapse_spaces:
            line = _SPACE_RE.sub(" ", line)
        if rules.trim_lines:
            line = line.strip()
        if rules.normalize_cjk_punctuation and _contains_cjk(line):
            line = line.translate(_CJK_PUNCTUATION)
        output.append(line)
    return "\n".join(output).strip() if rules.trim_lines else "\n".join(output)


def _contains_cjk(value: str) -> bool:
    return any(
        "\u3400" <= character <= "\u9fff" or "\uf900" <= character <= "\ufaff"
        for character in value
    )


def _join_text(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    separator = "" if _contains_cjk(left[-1:] + right[:1]) else " "
    return f"{left.rstrip()}{separator}{right.lstrip()}"


def _join_optional(left: str | None, right: str | None) -> str | None:
    if left is None and right is None:
        return None
    return _join_text(left or "", right or "")


def _parse_timestamp(value: str) -> int:
    hours, minutes, seconds_ms = value.replace(",", ".").split(":")
    seconds, milliseconds = seconds_ms.split(".")
    return int(hours) * 3_600_000 + int(minutes) * 60_000 + int(seconds) * 1_000 + int(milliseconds)


def _srt_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(max(0, milliseconds), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _vtt_timestamp(milliseconds: int) -> str:
    return _srt_timestamp(milliseconds).replace(",", ".")


def _ass_timestamp(milliseconds: int) -> str:
    total_centiseconds = round(max(0, milliseconds) / 10)
    hours, remainder = divmod(total_centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    seconds, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def _ass_color(rgb: str) -> str:
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", rgb):
        raise SubtitleError(f"ASS 色彩格式無效：{rgb}")
    red, green, blue = rgb[1:3], rgb[3:5], rgb[5:7]
    return f"&H00{blue}{green}{red}".upper()


def _ass_header(style: AssStyle) -> str:
    return (
        "[Script Info]\n"
        "; Generated by HearFlow Studio\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "PlayResX: 1920\n"
        "PlayResY: 1080\n"
        "\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{style.font_name},{style.font_size},"
        f"{_ass_color(style.primary_color)},&H000000FF,"
        f"{_ass_color(style.outline_color)},&H64000000,"
        f"0,0,0,0,100,100,0,0,1,{style.outline},{style.shadow},"
        f"{style.alignment},40,40,{style.margin_vertical},1\n"
        "\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )


def _versioned_path(path: Path, version_existing: bool) -> Path:
    destination = path.expanduser().resolve(strict=False)
    if not destination.exists():
        return destination
    if not version_existing:
        raise FileExistsError(destination)
    for version in range(2, 10_000):
        candidate = destination.with_name(f"{destination.stem}.v{version}{destination.suffix}")
        if not candidate.exists():
            return candidate
    raise SubtitleError("找不到可用的版本化輸出檔名。")


def export_many(
    service: SubtitleDocumentService,
    segments: Sequence[SubtitleSegment],
    options: Iterable[ExportOptions],
) -> list[Path]:
    """Export multiple requested formats with the same reviewed segment set."""

    return [service.export(segments, option) for option in options]
