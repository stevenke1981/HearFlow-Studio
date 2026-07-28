from __future__ import annotations

import json
from pathlib import Path

import pytest

from hearflow.domain.models import CleanRules, ExportOptions, QaRules, SubtitleSegment
from hearflow.services.subtitles import SubtitleDocumentService, SubtitleError, SubtitleQaService


def test_cleanup_is_non_mutating_and_normalizes_cjk_punctuation() -> None:
    source = [
        SubtitleSegment(
            "1",
            0,
            1_000,
            "  Ｈｅｌｌｏ   中文?  ",
            translated_text="  台灣  中文! ",
            metadata={"raw": True},
        )
    ]

    cleaned = SubtitleDocumentService().clean(source, CleanRules())

    assert cleaned[0].source_text == "Hello 中文？"
    assert cleaned[0].translated_text == "台灣 中文！"
    assert cleaned[0].status == "cleaned"
    assert cleaned[0].metadata == {"raw": True, "cleaned": True}
    assert source[0].source_text == "  Ｈｅｌｌｏ   中文?  "
    assert "cleaned" not in source[0].metadata


def test_qa_reports_blocking_timing_and_translation_coverage() -> None:
    issues = SubtitleQaService().inspect(
        [
            SubtitleSegment("bad", -1, -1, ""),
            SubtitleSegment("bad", 0, 100, "Very long text"),
        ],
        QaRules(
            max_chars_per_line=5,
            max_cps=2,
            require_translation=True,
        ),
    )
    codes = [issue.code for issue in issues]

    assert "duplicate_id" in codes
    assert "invalid_timing" in codes
    assert "missing_translation" in codes
    assert "long_line" in codes
    assert any(issue.blocks_export for issue in issues)


def test_export_all_formats_is_utf8_and_versions_existing(tmp_path: Path) -> None:
    service = SubtitleDocumentService()
    segments = [
        SubtitleSegment(
            "1",
            0,
            1_234,
            "source",
            translated_text="繁體字幕",
            metadata={"timestamp_accuracy": "chunk"},
        )
    ]
    extensions = (".srt", ".vtt", ".ass", ".txt", ".json")
    first_outputs: list[Path] = []
    for extension in extensions:
        first_outputs.append(
            service.export(
                segments,
                ExportOptions(
                    output_path=tmp_path / f"caption{extension}",
                    prefer_translation=True,
                    language="zh-TW",
                ),
            )
        )

    srt = first_outputs[0].read_text("utf-8")
    vtt = first_outputs[1].read_text("utf-8")
    ass = first_outputs[2].read_text("utf-8")
    payload = json.loads(first_outputs[4].read_text("utf-8"))
    assert "00:00:00,000 --> 00:00:01,234" in srt
    assert "繁體字幕" in srt
    assert vtt.startswith("WEBVTT\n")
    assert "timestamp_accuracy=chunk" in vtt
    assert "[V4+ Styles]" in ass and "Dialogue: 0," in ass
    assert payload["language"] == "zh-TW"
    assert payload["segments"][0]["translated_text"] == "繁體字幕"

    versioned = service.export(
        segments,
        ExportOptions(
            output_path=tmp_path / "caption.srt",
            prefer_translation=True,
        ),
    )
    assert versioned.name == "caption.v2.srt"
    assert (tmp_path / "caption.srt").read_text("utf-8") == srt


def test_require_translation_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(SubtitleError, match="尚未翻譯"):
        SubtitleDocumentService().export(
            [SubtitleSegment("1", 0, 1_000, "source")],
            ExportOptions(
                output_path=tmp_path / "caption.srt",
                prefer_translation=True,
                require_translation=True,
            ),
        )
