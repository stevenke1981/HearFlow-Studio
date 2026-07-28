from __future__ import annotations

from pathlib import Path

import pytest

from hearflow.domain.models import AsrResult, SubtitleSegment


@pytest.fixture
def media_file(tmp_path: Path) -> Path:
    path = tmp_path / "測試 聲音.wav"
    path.write_bytes(b"RIFF" + b"\x00" * 128)
    return path


@pytest.fixture
def asr_result() -> AsrResult:
    segments = [
        SubtitleSegment(
            id="0",
            start_ms=0,
            end_ms=1_250,
            source_text="Hello world.",
            metadata={"seek": 0, "timestamp_accuracy": "chunk"},
        ),
        SubtitleSegment(
            id="1",
            start_ms=1_250,
            end_ms=2_800,
            source_text="字幕測試！",
            metadata={"seek": 0, "timestamp_accuracy": "chunk"},
        ),
    ]
    return AsrResult(
        raw_response={
            "text": "Hello world. 字幕測試！",
            "language": "English",
            "duration": 2.8,
            "segments": [
                {"id": 0, "seek": 0, "start": 0.0, "end": 1.25, "text": "Hello world."},
                {"id": 1, "seek": 0, "start": 1.25, "end": 2.8, "text": "字幕測試！"},
            ],
            "timestamp_accuracy": "chunk",
        },
        response_headers={
            "x-asr-model": "qwen3-asr-0.6b",
            "x-asr-chunks": "2",
            "x-asr-timestamp-accuracy": "chunk",
        },
        text="Hello world. 字幕測試！",
        language="English",
        duration=2.8,
        segments=segments,
        model="qwen3-asr-0.6b",
        chunk_count=2,
        timestamp_accuracy="chunk",
        elapsed_seconds=0.25,
    )
