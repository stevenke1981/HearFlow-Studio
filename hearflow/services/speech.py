"""Speech synthesis boundary and long-transcript rendering helpers."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from hearflow.domain.models import SubtitleSegment
from hearflow.services.gateway import CancellationToken, RequestCancelled
from hearflow.services.settings import SpeechSettings


class SpeechError(RuntimeError):
    """Raised when speech generation is disabled or cannot be completed."""


class SpeechSynthesizer(Protocol):
    """Common local/remote text-to-speech adapter."""

    settings: SpeechSettings

    @property
    def output_extension(self) -> str: ...

    def synthesize(
        self,
        text: str,
        output_path: str | Path,
        *,
        language: str | None = None,
        voice: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> Path: ...


class DisabledSpeechSynthesizer:
    """Explicitly disabled speech service."""

    def __init__(self, settings: SpeechSettings | None = None) -> None:
        self.settings = settings or SpeechSettings()

    @property
    def output_extension(self) -> str:
        return self.settings.output_format or "wav"

    def synthesize(
        self,
        text: str,
        output_path: str | Path,
        *,
        language: str | None = None,
        voice: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> Path:
        del text, output_path, language, voice, cancellation
        raise SpeechError("TTS 尚未啟用；請在偏好設定選擇本機 Qwen3-TTS 或遠端供應商。")


class SpeechRenderService:
    """Render subtitle text in bounded requests and concatenate the audio safely."""

    def __init__(self, synthesizer: SpeechSynthesizer, *, ffmpeg_bin: str = "ffmpeg") -> None:
        self.synthesizer = synthesizer
        self.ffmpeg_bin = ffmpeg_bin

    def render_segments(
        self,
        segments: Sequence[SubtitleSegment],
        output_path: str | Path,
        *,
        prefer_translation: bool = True,
        language: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> Path:
        token = cancellation or CancellationToken()
        token.raise_if_cancelled()
        texts = [
            segment.display_text(prefer_translation=prefer_translation).strip()
            for segment in segments
        ]
        texts = [text for text in texts if text]
        if not texts:
            raise SpeechError("目前字幕沒有可供朗讀的文字。")
        chunks = _chunk_texts(texts, self.synthesizer.settings.max_chars_per_request)
        destination = Path(output_path).expanduser().resolve(strict=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if len(chunks) == 1:
            return self.synthesizer.synthesize(
                chunks[0],
                destination,
                language=language,
                cancellation=token,
            )

        extension = self.synthesizer.output_extension.lstrip(".") or "wav"
        with tempfile.TemporaryDirectory(prefix="hearflow-tts-") as raw_temp:
            temp = Path(raw_temp)
            clips: list[Path] = []
            for index, chunk in enumerate(chunks, start=1):
                token.raise_if_cancelled()
                clip = temp / f"clip-{index:05d}.{extension}"
                clips.append(
                    self.synthesizer.synthesize(
                        chunk,
                        clip,
                        language=language,
                        cancellation=token,
                    )
                )
            token.raise_if_cancelled()
            self._concatenate(clips, destination, token)
        return destination

    def _concatenate(
        self,
        clips: Sequence[Path],
        destination: Path,
        cancellation: CancellationToken,
    ) -> None:
        partial = destination.with_name(destination.stem + ".partial" + destination.suffix)
        partial.unlink(missing_ok=True)
        if destination.suffix.casefold() == ".pcm":
            with partial.open("wb") as output:
                for clip in clips:
                    cancellation.raise_if_cancelled()
                    output.write(clip.read_bytes())
            if partial.stat().st_size == 0:
                partial.unlink(missing_ok=True)
                raise SpeechError("TTS 回傳了空白 PCM 音訊。")
            partial.replace(destination)
            return
        list_file = clips[0].parent / "concat.txt"
        list_file.write_text(
            "".join(f"file '{_ffconcat_path(path)}'\n" for path in clips),
            encoding="utf-8",
        )
        codec_args = _codec_args(destination.suffix.casefold())
        command = [
            self.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-vn",
            *codec_args,
            "-y",
            str(partial),
        ]
        creationflags = 0x08000000 if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        try:
            while process.poll() is None:
                if cancellation.cancelled:
                    process.terminate()
                    try:
                        process.wait(timeout=3.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3.0)
                    raise RequestCancelled()
                time.sleep(0.05)
            stdout, stderr = process.communicate()
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        if process.returncode != 0:
            partial.unlink(missing_ok=True)
            detail = (stderr or stdout or "FFmpeg 未提供錯誤訊息").strip()
            raise SpeechError(f"無法串接 TTS 音訊（exit {process.returncode}）：{detail[-1600:]}")
        if not partial.is_file() or partial.stat().st_size == 0:
            partial.unlink(missing_ok=True)
            raise SpeechError("FFmpeg 已完成，但沒有產生有效的 TTS 音訊。")
        partial.replace(destination)


def _chunk_texts(texts: Sequence[str], limit: int) -> list[str]:
    if limit < 100:
        raise ValueError("max_chars_per_request must be at least 100")
    result: list[str] = []
    current = ""
    for text in texts:
        pieces = _split_long_text(text, limit)
        for piece in pieces:
            candidate = f"{current}\n{piece}".strip() if current else piece
            if current and len(candidate) > limit:
                result.append(current)
                current = piece
            else:
                current = candidate
    if current:
        result.append(current)
    return result


def _split_long_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    result: list[str] = []
    remaining = text
    separators = ("。", "！", "？", ". ", "! ", "? ", "，", ", ", " ")
    while len(remaining) > limit:
        boundary = -1
        for separator in separators:
            position = remaining.rfind(separator, 0, limit + 1)
            if position > boundary:
                boundary = position + len(separator)
        if boundary < max(40, limit // 3):
            boundary = limit
        result.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].strip()
    if remaining:
        result.append(remaining)
    return result


def _ffconcat_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''").replace("\\", "/")


def _codec_args(suffix: str) -> list[str]:
    if suffix == ".wav":
        return ["-c:a", "pcm_s16le", "-ar", "24000"]
    if suffix == ".mp3":
        return ["-c:a", "libmp3lame", "-q:a", "2"]
    if suffix in {".m4a", ".aac"}:
        return ["-c:a", "aac", "-b:a", "192k"]
    if suffix == ".flac":
        return ["-c:a", "flac"]
    if suffix == ".opus":
        return ["-c:a", "libopus", "-b:a", "128k"]
    raise SpeechError(f"不支援串接的 TTS 輸出格式：{suffix or '(無副檔名)'}")
