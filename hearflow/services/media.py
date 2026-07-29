"""Safe FFprobe and FFmpeg integration."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from hearflow.domain.models import JsonValue, MediaMetadata
from hearflow.domain.validation import ensure_output_is_not_source, validate_media_path
from hearflow.services.fsutil import CREATE_NO_WINDOW, sha256_file

ALLOWED_EXTENSIONS = frozenset(
    {
        ".wav",
        ".mp3",
        ".m4a",
        ".mp4",
        ".mpeg",
        ".mpga",
        ".webm",
        ".ogg",
        ".opus",
        ".flac",
        ".aac",
        ".mov",
        ".mkv",
    }
)


class MediaError(RuntimeError):
    """Raised when media cannot be inspected or converted."""


class MediaInspector:
    """Inspect real media with FFprobe and create safe FFmpeg outputs."""

    def __init__(
        self,
        *,
        ffprobe_bin: str | Path = "ffprobe",
        ffmpeg_bin: str | Path = "ffmpeg",
        timeout_seconds: float = 60.0,
    ) -> None:
        self.ffprobe_bin = _resolve_tool(ffprobe_bin)
        self.ffmpeg_bin = _resolve_tool(ffmpeg_bin)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds

    def probe(self, path: str | Path) -> MediaMetadata:
        """Return validated FFprobe metadata for one supported media file."""

        media_path = validate_media_path(path, allowed_extensions=ALLOWED_EXTENSIONS)
        command = [
            self.ffprobe_bin,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "--",
            str(media_path),
        ]
        completed = _run(command, timeout=self.timeout_seconds)
        if completed.returncode != 0:
            raise MediaError(_process_error("FFprobe 無法讀取媒體", completed))
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise MediaError("FFprobe 回應不是有效 JSON。") from exc
        if not isinstance(payload, dict):
            raise MediaError("FFprobe 回應格式無效。")
        streams = payload.get("streams")
        if not isinstance(streams, list):
            raise MediaError("FFprobe 回應缺少 streams。")
        audio_streams = tuple(
            _json_mapping(stream)
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "audio"
        )
        video_streams = tuple(
            _json_mapping(stream)
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
        )
        if not audio_streams:
            raise MediaError("媒體沒有可用的音訊軌。")
        format_payload = payload.get("format")
        if not isinstance(format_payload, dict):
            format_payload = {}
        duration = _first_duration(format_payload, audio_streams)
        format_name = str(format_payload.get("format_name") or media_path.suffix[1:])
        bit_rate = _optional_positive_int(format_payload.get("bit_rate"))
        return MediaMetadata(
            path=media_path,
            duration_seconds=duration,
            format_name=format_name,
            audio_streams=audio_streams,
            video_streams=video_streams,
            size_bytes=media_path.stat().st_size,
            bit_rate=bit_rate,
        )

    def burn_ass(
        self,
        media_path: str | Path,
        ass_path: str | Path,
        output_path: str | Path,
        *,
        overwrite: bool = False,
        timeout_seconds: float | None = None,
    ) -> Path:
        """Render ASS subtitles into a new MP4 and verify the result."""

        source = validate_media_path(media_path, allowed_extensions=ALLOWED_EXTENSIONS)
        subtitle = Path(ass_path).expanduser().resolve(strict=True)
        destination = Path(output_path).expanduser().resolve(strict=False)
        ensure_output_is_not_source(source, destination)
        if destination.exists() and not overwrite:
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.stem}.{uuid.uuid4().hex}.tmp{destination.suffix}"
        )
        original_digest = sha256_file(source)
        filter_value = f"ass='{escape_ffmpeg_filter_path(subtitle)}'"
        command = [
            self.ffmpeg_bin,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-vf",
            filter_value,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-preset",
            "medium",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(temporary),
        ]
        try:
            completed = _run(
                command,
                timeout=timeout_seconds or max(self.timeout_seconds, 3_600),
            )
            if completed.returncode != 0:
                raise MediaError(_process_error("FFmpeg 字幕壓製失敗", completed))
            if not temporary.is_file() or temporary.stat().st_size <= 0:
                raise MediaError("FFmpeg 未產生有效輸出。")
            output_metadata = self.probe(temporary)
            if not output_metadata.video_streams:
                raise MediaError("壓製結果缺少影像軌。")
            if sha256_file(source) != original_digest:
                raise MediaError("原始媒體在壓製期間遭到修改。")
            os.replace(temporary, destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    def environment_report(self) -> dict[str, Any]:
        """Return executable paths and version probes for the UI."""

        report: dict[str, Any] = {
            "ffprobe": self.ffprobe_bin,
            "ffmpeg": self.ffmpeg_bin,
        }
        for label, executable in (
            ("ffprobe_version", self.ffprobe_bin),
            ("ffmpeg_version", self.ffmpeg_bin),
        ):
            result = _run([executable, "-version"], timeout=10)
            report[label] = (
                result.stdout.splitlines()[0]
                if result.returncode == 0 and result.stdout
                else "unavailable"
            )
        return report


def discover_media(paths: Iterable[str | Path], *, recursive: bool = True) -> list[Path]:
    """Expand files/directories into a de-duplicated, stable media list."""

    discovered: dict[str, Path] = {}
    for raw in paths:
        path = Path(raw).expanduser().resolve(strict=True)
        candidates: Iterable[Path]
        if path.is_file():
            candidates = (path,)
        elif path.is_dir():
            candidates = path.rglob("*") if recursive else path.glob("*")
        else:
            continue
        for candidate in candidates:
            if candidate.is_file() and candidate.suffix.casefold() in ALLOWED_EXTENSIONS:
                resolved = candidate.resolve()
                discovered[str(resolved).casefold()] = resolved
    return sorted(discovered.values(), key=lambda item: str(item).casefold())


def escape_ffmpeg_filter_path(path: str | Path) -> str:
    """Escape an absolute path for an FFmpeg filter argument."""

    value = Path(path).resolve(strict=False).as_posix()
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def _resolve_tool(value: str | Path) -> str:
    text = str(value)
    candidate = Path(text).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        return str(resolved)
    located = shutil.which(text)
    if located is None:
        raise FileNotFoundError(f"找不到必要工具：{text}")
    return located


def _run(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"外部媒體工具逾時：{command[0]}") from exc
    except OSError as exc:
        raise MediaError(f"無法啟動外部媒體工具：{command[0]}") from exc


def _process_error(prefix: str, completed: subprocess.CompletedProcess[str]) -> str:
    detail = (completed.stderr or completed.stdout or "").strip().splitlines()
    safe_detail = detail[-1][:500] if detail else f"exit {completed.returncode}"
    return f"{prefix}：{safe_detail}"


def _first_duration(
    format_payload: dict[str, Any],
    audio_streams: tuple[dict[str, JsonValue], ...],
) -> float:
    candidates: list[object] = [format_payload.get("duration")]
    candidates.extend(stream.get("duration") for stream in audio_streams)
    for value in candidates:
        try:
            duration = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            return duration
    raise MediaError("媒體時長無效或為零。")


def _optional_positive_int(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _json_mapping(value: dict[str, Any]) -> dict[str, JsonValue]:
    encoded = json.dumps(value, ensure_ascii=False)
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise MediaError("FFprobe stream 格式無效。")
    return decoded
