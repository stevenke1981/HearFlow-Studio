"""Local Qwen3-TTS adapter using the CUDA-enabled qwen3tts-rs CLI."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from hearflow.services.gateway import CancellationToken, RequestCancelled
from hearflow.services.settings import SpeechSettings


class LocalSpeechError(RuntimeError):
    """Raised when the local Qwen3-TTS engine cannot synthesize audio."""


class Qwen3TtsCliSynthesizer:
    """Invoke a prebuilt ``qwen3tts-synthesize``/``qwen3tts-rs`` executable.

    CUDA is selected when the executable was built with the qwen3tts-rs
    ``candle-llm cuda`` features.  HearFlow does not pretend that llama.cpp runs
    Qwen3-TTS; ASR and translation use llama.cpp, while TTS uses Candle/CUDA.
    """

    def __init__(
        self,
        settings: SpeechSettings,
        *,
        runtime_root: str | Path = "runtime",
    ) -> None:
        self.settings = settings
        self.runtime_root = Path(runtime_root).expanduser().resolve(strict=False)
        if settings.output_format != "wav":
            raise ValueError("本機 Qwen3-TTS 目前只輸出 WAV；請將 output_format 設為 wav。")

    @property
    def output_extension(self) -> str:
        return "wav"

    def synthesize(
        self,
        text: str,
        output_path: str | Path,
        *,
        language: str | None = None,
        voice: str | None = None,
        instructions: str | None = None,
        reference_audio: str | Path | None = None,
        reference_text: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> Path:
        normalized = text.strip()
        if not normalized:
            raise LocalSpeechError("TTS 文字不可留空。")
        token = cancellation or CancellationToken()
        token.raise_if_cancelled()

        executable = self.resolve_executable()
        destination = Path(output_path).expanduser().resolve(strict=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.stem + ".partial" + destination.suffix)
        partial.unlink(missing_ok=True)

        command = [
            str(executable),
            "--text",
            normalized,
            "--output",
            str(partial),
            "--model",
            self.settings.model,
            "--backend",
            self.settings.local_backend,
            "--language",
            (language or self.settings.language or "auto"),
            "--speed",
            f"{self.settings.speed:g}",
        ]
        model_dir = self.resolve_model_dir(required=False)
        if model_dir is not None:
            command.extend(["--model-dir", str(model_dir)])
        selected_voice = (voice or self.settings.voice).strip()
        if selected_voice:
            command.extend(["--speaker", selected_voice])
        selected_instructions = (instructions or self.settings.instructions).strip()
        if selected_instructions:
            command.extend(["--instruct", selected_instructions])
        if self.settings.local_talker_backend:
            command.extend(["--talker-backend", self.settings.local_talker_backend])
        if self.settings.local_talker_gguf.strip():
            command.extend(["--talker-gguf", self.settings.local_talker_gguf.strip()])
        if reference_audio is not None:
            command.extend(["--reference-audio", str(Path(reference_audio).expanduser())])
        if reference_text:
            command.extend(["--reference-text", reference_text])

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
                if token.cancelled:
                    self._terminate(process)
                    raise RequestCancelled()
                time.sleep(0.05)
            stdout, stderr = process.communicate()
        except BaseException:
            if process.poll() is None:
                self._terminate(process)
            partial.unlink(missing_ok=True)
            raise

        if process.returncode != 0:
            partial.unlink(missing_ok=True)
            detail = (stderr or stdout or "qwen3tts-rs 未提供錯誤訊息").strip()
            raise LocalSpeechError(
                f"Qwen3-TTS 產生失敗（exit {process.returncode}）：{detail[-2000:]}"
            )
        if not partial.is_file() or partial.stat().st_size == 0:
            partial.unlink(missing_ok=True)
            raise LocalSpeechError("Qwen3-TTS 已結束，但沒有產生有效音訊檔。")
        partial.replace(destination)
        return destination

    def resolve_executable(self) -> Path:
        """Find a configured, bundled, or PATH-installed qwen3tts-rs CLI."""

        candidates: list[Path] = []
        if self.settings.local_executable.strip():
            candidates.append(Path(self.settings.local_executable).expanduser())
        candidates.extend(
            [
                self.runtime_root / "tts" / "qwen3tts-synthesize.exe",
                self.runtime_root / "tts" / "qwen3tts-rs.exe",
                self.runtime_root / "tts" / "qwen3tts-synthesize",
                self.runtime_root / "tts" / "qwen3tts-rs",
            ]
        )
        for name in (
            "qwen3tts-synthesize.exe",
            "qwen3tts-rs.exe",
            "qwen3tts-synthesize",
            "qwen3tts-rs",
        ):
            resolved = shutil.which(name)
            if resolved:
                candidates.append(Path(resolved))
        for candidate in candidates:
            resolved = candidate.resolve(strict=False)
            if resolved.is_file():
                return resolved
        raise LocalSpeechError(
            "找不到 qwen3tts-rs 執行檔。請執行 scripts/install-tts.ps1；"
            '該安裝器會以 --features "candle-llm cuda" 建置 NVIDIA CUDA 版本。'
        )

    def resolve_model_dir(self, *, required: bool) -> Path | None:
        configured = self.settings.local_model_dir.strip()
        candidates: list[Path] = []
        if configured:
            candidates.append(Path(configured).expanduser())
        env_dir = os.environ.get("QWEN3_TTS_MODEL_DIR", "").strip()
        if env_dir:
            candidates.append(Path(env_dir).expanduser())
        model_slug = self.settings.model.rsplit("/", 1)[-1]
        candidates.extend(
            [
                self.runtime_root / "models" / "tts" / model_slug,
                self.runtime_root / "models" / "tts",
            ]
        )
        for candidate in candidates:
            resolved = candidate.resolve(strict=False)
            if resolved.is_dir() and (
                (resolved / "model.safetensors").is_file()
                or any(resolved.glob("*.gguf"))
                or (resolved / "config.json").is_file()
            ):
                return resolved
        if required:
            raise LocalSpeechError("找不到 Qwen3-TTS 模型目錄；請重新執行 TTS 安裝器。")
        return None

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        process.terminate()
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3.0)
