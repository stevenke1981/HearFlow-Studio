"""Managed llama.cpp translation engine (TranslateGemma) process lifecycle.

This module manages a dedicated llama-server instance that serves an
OpenAI-compatible ``/v1/chat/completions`` endpoint for translation.  It reuses
the same llama-server binary as the ASR engine but loads a different model
(TranslateGemma / Gemma-3-4B-IT) on a separate port.
"""

from __future__ import annotations

import logging
import os
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from hearflow.services.fsutil import CREATE_NO_WINDOW
from hearflow.services.settings import TranslationEngineSettings

logger = logging.getLogger(__name__)


class TranslationEngineError(RuntimeError):
    """Raised when the managed translation engine cannot be started or controlled."""


@dataclass(frozen=True, slots=True)
class TranslationEnginePaths:
    """Resolved runtime files for the translation engine."""

    runtime: Path
    llama_server: Path
    model: Path | None


@dataclass(slots=True)
class TranslationProcess:
    """The managed translation llama-server child process."""

    process: subprocess.Popen[bytes]
    log_file: Any


def _application_root() -> Path:
    import sys

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


class TranslationEngineManager:
    """Start, inspect, and stop a dedicated llama-server for translation."""

    def __init__(
        self,
        settings: TranslationEngineSettings,
        *,
        runtime_root: str | Path | None = None,
        api_key: str | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_root = (
            Path(runtime_root or _application_root() / "runtime").expanduser().resolve(strict=False)
        )
        self.api_key = api_key or secrets.token_urlsafe(32)
        self.process: TranslationProcess | None = None
        self._lock = threading.RLock()

    @property
    def base_url(self) -> str:
        """Return the OpenAI-compatible base URL for the translation server."""

        return f"http://127.0.0.1:{self.settings.port}/v1"

    def resolve_paths(self) -> TranslationEnginePaths:
        """Locate the llama-server binary and translation model."""

        runtime = self.runtime_root
        executable = "llama-server.exe" if os.name == "nt" else "llama-server"
        backend_dir = {
            "cuda": "cuda12.4",
            "vulkan": "vulkan",
            "cpu": "cpu",
        }[self.settings.backend]
        candidates = [
            runtime / "engine" / backend_dir / executable,
            runtime / "engine" / executable,
        ]
        llama = next((p for p in candidates if p.is_file()), candidates[0])

        translation_model_dir = runtime / "models" / "translation"
        legacy_model_dir = runtime / "models"
        normalized_model = self.settings.model_id.replace("-", "").casefold()
        models: list[Path] = []
        for model_dir in (translation_model_dir, legacy_model_dir):
            candidates = sorted(
                path
                for path in model_dir.rglob("*.gguf")
                if "mmproj" not in path.name.casefold()
                and (
                    normalized_model in path.name.replace("-", "").casefold()
                    or "gemma" in path.name.casefold()
                    or "translate" in path.name.casefold()
                )
            )
            if candidates:
                models = candidates
                break
        return TranslationEnginePaths(
            runtime=runtime,
            llama_server=llama,
            model=models[0] if models else None,
        )

    def installation_status(self) -> dict[str, Any]:
        """Return a non-mutating readiness report."""

        paths = self.resolve_paths()
        return {
            "runtime": str(paths.runtime),
            "llama_server": str(paths.llama_server),
            "llama_server_exists": paths.llama_server.is_file(),
            "model": None if paths.model is None else str(paths.model),
            "model_ready": paths.model is not None,
        }

    def health(self) -> dict[str, Any]:
        """Check whether the translation server is live and ready."""

        try:
            live = httpx.get(
                f"http://127.0.0.1:{self.settings.port}/health",
                timeout=3.0,
            )
            live_ok = live.status_code == 200
        except httpx.HTTPError:
            live_ok = False
        return {
            "live": live_ok,
            "ready": live_ok,
            "port": self.settings.port,
            "model_id": self.settings.model_id,
        }

    def start(self) -> TranslationProcess:
        """Start the translation llama-server and wait until ready."""

        logger.info(
            "正在啟動翻譯引擎（model=%s, port=%d）",
            self.settings.model_id,
            self.settings.port,
        )
        with self._lock:
            return self._start_locked()

    def _start_locked(self) -> TranslationProcess:
        if not self.settings.enabled:
            raise TranslationEngineError("翻譯引擎未啟用。")
        if self.process is not None:
            if self.process.process.poll() is None:
                return self.process
            self._stop_locked()

        paths = self.resolve_paths()
        if not paths.llama_server.is_file():
            raise TranslationEngineError(
                f"找不到 llama-server：{paths.llama_server}\n請先安裝本機引擎。"
            )
        if paths.model is None:
            raise TranslationEngineError(
                "找不到翻譯模型（TranslateGemma / Gemma-3-4B-IT）。\n"
                "請執行 .\\scripts\\install-engine.ps1 -InstallTranslationModel"
            )

        log_dir = paths.runtime / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = (log_dir / "managed-translation.log").open("ab", buffering=0)

        gpu_layers = 0 if self.settings.backend == "cpu" else self.settings.gpu_layers
        args = [
            str(paths.llama_server),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.settings.port),
            "--alias",
            self.settings.model_id,
            "-m",
            str(paths.model),
            "--ctx-size",
            str(self.settings.context_size),
            "--batch-size",
            str(self.settings.batch_size),
            "--ubatch-size",
            str(self.settings.micro_batch_size),
            "--threads",
            str(self.settings.threads),
            "--threads-batch",
            str(self.settings.threads),
            "--parallel",
            "1",
            "--n-gpu-layers",
            str(gpu_layers),
            "--timeout",
            "3600",
            "--jinja",
            "--metrics",
            "--no-webui",
            "--api-key",
            self.api_key,
        ]
        if self.settings.backend == "cpu":
            args.extend(["--n-gpu-layers", "0"])

        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=str(paths.runtime),
                creationflags=CREATE_NO_WINDOW,
            )
            self.process = TranslationProcess(process, log_file)
            _wait_http(
                f"http://127.0.0.1:{self.settings.port}/health",
                timeout=180,
                process=process,
            )
        except BaseException:
            if process is not None:
                _terminate_process(process)
            self.process = None
            log_file.close()
            raise
        logger.info("翻譯引擎已就緒（port=%d）", self.settings.port)
        return self.process

    def stop(self) -> None:
        """Stop the translation llama-server."""

        logger.info("正在停止翻譯引擎")
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self.process is None:
            return
        _terminate_process(self.process.process)
        self.process.log_file.close()
        self.process = None

    def restart(self) -> TranslationProcess:
        with self._lock:
            self._stop_locked()
            return self._start_locked()


def _wait_http(
    url: str,
    *,
    timeout: float,
    process: subprocess.Popen[bytes],
) -> None:
    deadline = time.monotonic() + timeout
    last_error = "尚未回應"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise TranslationEngineError(f"翻譯引擎在啟動期間結束（exit {process.returncode}）。")
        try:
            response = httpx.get(url, timeout=2.0)
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise TranslationEngineError(f"翻譯引擎啟動逾時：{last_error}")


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
    else:
        import signal

        process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
