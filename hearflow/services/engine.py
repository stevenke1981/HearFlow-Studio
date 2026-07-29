"""Managed llama.cpp and qwen3-asr gateway process lifecycle."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from hearflow.services.fsutil import CREATE_NO_WINDOW, sha256_file
from hearflow.services.gateway import QwenGatewayClient
from hearflow.services.settings import EngineSettings

logger = logging.getLogger(__name__)


class EngineError(RuntimeError):
    """Raised when the managed engine cannot be installed or controlled."""


@dataclass(frozen=True, slots=True)
class EnginePaths:
    """Resolved runtime files needed by the managed engine."""

    runtime: Path
    llama_server: Path
    gateway: Path
    ffmpeg: Path
    ffprobe: Path
    model: Path | None
    mmproj: Path | None


@dataclass(slots=True)
class ManagedProcesses:
    """The two managed child processes and their log streams."""

    llama: subprocess.Popen[bytes]
    gateway: subprocess.Popen[bytes]
    llama_log: Any
    gateway_log: Any


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    executable: str
    creation_ticks: int


def application_root() -> Path:
    """Locate source checkout or packaged application directory."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


class EngineManager:
    """Start, inspect, and stop only processes launched by HearFlow."""

    def __init__(
        self,
        settings: EngineSettings,
        *,
        runtime_root: str | Path | None = None,
        public_api_key: str | None = None,
        internal_api_key: str | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_root = (
            Path(runtime_root or application_root() / settings.runtime_dir)
            .expanduser()
            .resolve(strict=False)
        )
        self.public_api_key = public_api_key or secrets.token_urlsafe(32)
        self.internal_api_key = internal_api_key or secrets.token_urlsafe(32)
        self.processes: ManagedProcesses | None = None
        self._operation_lock = threading.RLock()

    @property
    def client(self) -> QwenGatewayClient:
        return QwenGatewayClient(
            self.settings.gateway_url,
            api_key=self.public_api_key,
            timeout=120.0,
        )

    def resolve_paths(self) -> EnginePaths:
        """Resolve either source-layout or packaged-layout runtime files."""

        runtime = self.runtime_root
        executable_name = "llama-server.exe" if os.name == "nt" else "llama-server"
        gateway_name = "qwen3-asr-gateway.exe" if os.name == "nt" else "qwen3-asr-gateway"
        backend_directory = {
            "cuda": "cuda12.4",
            "vulkan": "vulkan",
            "cpu": "cpu",
        }[self.settings.backend]
        legacy_directory = {
            "cuda": "llama-cuda",
            "vulkan": "llama-vulkan",
            "cpu": "llama-cpu",
        }[self.settings.backend]
        candidates = [
            runtime / "engine" / backend_directory / executable_name,
            runtime / "engine" / executable_name,
            runtime / legacy_directory / executable_name,
        ]
        llama = next((item for item in candidates if item.is_file()), candidates[0])
        gateway_candidates = [
            runtime / "gateway" / gateway_name,
            runtime / "engine" / gateway_name,
            application_root()
            / "third_party"
            / "qwen3-asr-llama-cpp"
            / "gateway"
            / "target"
            / "release"
            / gateway_name,
        ]
        gateway = next(
            (item for item in gateway_candidates if item.is_file()),
            gateway_candidates[0],
        )
        ffmpeg = _tool_path(runtime, "ffmpeg")
        ffprobe = _tool_path(runtime, "ffprobe")
        asr_model_dir = runtime / "models" / "asr"
        models = sorted(
            item
            for item in asr_model_dir.glob("*.gguf")
            if "mmproj" not in item.name.casefold()
        )
        mmprojs = sorted(asr_model_dir.glob("*mmproj*.gguf"))
        # Backward-compatible legacy lookup.  It is deliberately restricted to
        # ASR-named files so a TranslateGemma GGUF can never be selected as the
        # Qwen3-ASR model merely because it sorts first.
        if not models or not mmprojs:
            legacy_model_dir = runtime / "models"
            legacy_models = sorted(
                item
                for item in legacy_model_dir.glob("*.gguf")
                if "mmproj" not in item.name.casefold()
                and ("qwen3-asr" in item.name.casefold() or "asr" in item.name.casefold())
                and "gemma" not in item.name.casefold()
                and "translation" not in item.name.casefold()
            )
            legacy_mmprojs = sorted(
                item
                for item in legacy_model_dir.glob("*mmproj*.gguf")
                if "qwen3-asr" in item.name.casefold() or "asr" in item.name.casefold()
            )
            if not models:
                models = legacy_models
            if not mmprojs:
                mmprojs = legacy_mmprojs
        return EnginePaths(
            runtime=runtime,
            llama_server=llama,
            gateway=gateway,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            model=models[0] if models else None,
            mmproj=mmprojs[0] if mmprojs else None,
        )

    def installation_status(self) -> dict[str, Any]:
        """Return a non-mutating runtime readiness report."""

        paths = self.resolve_paths()
        return {
            "runtime": str(paths.runtime),
            "llama_server": str(paths.llama_server),
            "llama_server_exists": paths.llama_server.is_file(),
            "gateway": str(paths.gateway),
            "gateway_exists": paths.gateway.is_file(),
            "ffmpeg": str(paths.ffmpeg),
            "ffmpeg_exists": paths.ffmpeg.is_file(),
            "ffprobe": str(paths.ffprobe),
            "ffprobe_exists": paths.ffprobe.is_file(),
            "model": None if paths.model is None else str(paths.model),
            "mmproj": None if paths.mmproj is None else str(paths.mmproj),
            "model_ready": paths.model is not None and paths.mmproj is not None,
        }

    def verify_installation(self) -> dict[str, Any]:
        """Verify every runtime manifest record before treating an install as usable."""

        status = self.installation_status()
        status["integrity_ready"] = False
        manifest_path = self.runtime_root / "manifest.json"
        status["manifest"] = str(manifest_path)
        if not manifest_path.is_file():
            status["integrity_detail"] = "找不到 runtime manifest。"
            return status

        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("runtime manifest 必須是 JSON object。")
            if payload.get("schema_version") != 1:
                raise ValueError("runtime manifest schema_version 不支援。")
            security = payload.get("security")
            if not isinstance(security, dict) or security.get("contains_credentials") is not False:
                raise ValueError("runtime manifest 的安全標記無效。")
            expected_backend = {
                "cuda": "cuda12.4",
                "vulkan": "vulkan",
                "cpu": "cpu",
            }[self.settings.backend]
            if payload.get("backend") != expected_backend:
                raise ValueError(
                    f"runtime 後端為 {payload.get('backend')!r}，目前需要 {expected_backend!r}。"
                )
            records = payload.get("files")
            if not isinstance(records, dict):
                raise ValueError("runtime manifest 缺少 files。")
            required = {
                "llama_server",
                "gateway",
                "model",
                "mmproj",
                "ffmpeg",
                "ffprobe",
            }
            if not required.issubset(records):
                missing = "、".join(sorted(required.difference(records)))
                raise ValueError(f"runtime manifest 缺少：{missing}")

            runtime = self.runtime_root.resolve(strict=True)
            selected_paths = self.resolve_paths()
            selected = {
                "llama_server": selected_paths.llama_server,
                "gateway": selected_paths.gateway,
                "model": selected_paths.model,
                "mmproj": selected_paths.mmproj,
                "ffmpeg": selected_paths.ffmpeg,
                "ffprobe": selected_paths.ffprobe,
            }
            for name in sorted(required):
                record = records[name]
                if not isinstance(record, dict):
                    raise ValueError(f"runtime manifest 記錄無效：{name}")
                relative = record.get("path")
                expected_size = record.get("bytes")
                expected_hash = record.get("sha256")
                if (
                    not isinstance(relative, str)
                    or not relative.strip()
                    or isinstance(expected_size, bool)
                    or not isinstance(expected_size, int)
                    or expected_size < 0
                    or not isinstance(expected_hash, str)
                    or len(expected_hash) != 64
                ):
                    raise ValueError(f"runtime manifest 欄位無效：{name}")
                target = (runtime / relative).resolve(strict=True)
                if not target.is_relative_to(runtime) or not target.is_file():
                    raise ValueError(f"runtime 檔案位於允許目錄之外：{name}")
                selected_target = selected[name]
                if selected_target is None or target != selected_target.resolve(strict=True):
                    raise ValueError(f"runtime manifest 與實際選用檔案不一致：{name}")
                if target.stat().st_size != expected_size:
                    raise ValueError(f"runtime 檔案大小不符：{name}")
                if sha256_file(target) != expected_hash.casefold():
                    raise ValueError(f"runtime SHA-256 不符：{name}")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            status["integrity_detail"] = str(exc)
            return status

        status["integrity_ready"] = True
        status["integrity_detail"] = "runtime manifest、大小與 SHA-256 均相符。"
        return status

    def start(self) -> ManagedProcesses:
        """Start managed llama-server and gateway, then wait until ready."""

        logger.info("正在啟動受管理引擎（backend=%s）", self.settings.backend)
        with self._operation_lock:
            return self._start_locked()

    def _start_locked(self) -> ManagedProcesses:
        if self.settings.mode != "managed":
            raise EngineError("外部 Gateway 模式不會由 HearFlow 啟動或停止。")
        if self.processes is not None:
            if self._children_alive():
                return self.processes
            self.stop()
        paths = self.resolve_paths()
        missing = [
            str(path)
            for path in (paths.llama_server, paths.gateway, paths.ffmpeg, paths.ffprobe)
            if not path.is_file()
        ]
        if missing:
            raise EngineError("本機引擎尚未安裝，缺少：" + "、".join(missing))
        _recover_stale_runtime(paths)
        paths.runtime.mkdir(parents=True, exist_ok=True)
        log_dir = paths.runtime / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        llama_log = (log_dir / "managed-llama.log").open("ab", buffering=0)
        gateway_log = (log_dir / "managed-gateway.log").open("ab", buffering=0)

        llama_args = [
            str(paths.llama_server),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.settings.llama_port),
            "--alias",
            self.settings.model_id,
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
            "99" if self.settings.backend in {"cuda", "vulkan"} else "0",
            "--timeout",
            "3600",
            "--jinja",
            "--metrics",
            "--no-webui",
            "--flash-attn",
            "auto" if self.settings.backend != "cpu" else "off",
            "--api-key",
            self.internal_api_key,
        ]
        if paths.model is not None and paths.mmproj is not None:
            llama_args.extend(["-m", str(paths.model), "--mmproj", str(paths.mmproj)])
        else:
            llama_args.extend(
                [
                    "-hf",
                    f"{self.settings.model_repository}:{self.settings.model_quant}",
                ]
            )
        if self.settings.backend == "cpu":
            llama_args.append("--no-mmproj-offload")

        environment = dict(os.environ)
        environment.update(
            {
                "BIND_ADDR": f"127.0.0.1:{self.settings.gateway_port}",
                "API_KEY": self.public_api_key,
                "LLAMA_API_KEY": self.internal_api_key,
                "LLAMA_URL": f"http://127.0.0.1:{self.settings.llama_port}",
                "MODEL_ID": self.settings.model_id,
                "FFMPEG_BIN": str(paths.ffmpeg),
                "FFPROBE_BIN": str(paths.ffprobe),
                "CHUNK_SECONDS": "30",
                "MAX_CONCURRENT_JOBS": "1",
            }
        )
        creationflags = CREATE_NO_WINDOW
        llama_process: subprocess.Popen[bytes] | None = None
        gateway_process: subprocess.Popen[bytes] | None = None
        try:
            llama_process = subprocess.Popen(
                llama_args,
                stdin=subprocess.DEVNULL,
                stdout=llama_log,
                stderr=subprocess.STDOUT,
                cwd=str(paths.runtime),
                creationflags=creationflags,
            )
            _wait_http(
                f"http://127.0.0.1:{self.settings.llama_port}/health",
                timeout=240,
                process=llama_process,
            )
            gateway_process = subprocess.Popen(
                [str(paths.gateway)],
                stdin=subprocess.DEVNULL,
                stdout=gateway_log,
                stderr=subprocess.STDOUT,
                cwd=str(paths.runtime),
                env=environment,
                creationflags=creationflags,
            )
            self.processes = ManagedProcesses(
                llama_process,
                gateway_process,
                llama_log,
                gateway_log,
            )
            _wait_gateway(
                self.client,
                timeout=90,
                processes=(llama_process, gateway_process),
            )
        except BaseException:
            if gateway_process is not None:
                _terminate_process(gateway_process)
            if llama_process is not None:
                _terminate_process(llama_process)
            self.processes = None
            llama_log.close()
            gateway_log.close()
            raise
        assert llama_process is not None
        assert gateway_process is not None
        try:
            owner_identity = _process_identity(os.getpid())
            llama_identity = _process_identity(llama_process.pid)
            gateway_identity = _process_identity(gateway_process.pid)
            if owner_identity is None or llama_identity is None or gateway_identity is None:
                raise EngineError("無法取得受管理程序的安全身分資訊。")
            _atomic_json(
                paths.runtime / "managed-processes.json",
                {
                    "llama_pid": llama_process.pid,
                    "gateway_pid": gateway_process.pid,
                    "started_at": time.time(),
                    "owner_pid": os.getpid(),
                    "owner_executable": owner_identity.executable,
                    "owner_creation_ticks": owner_identity.creation_ticks,
                    "llama_executable": llama_identity.executable,
                    "llama_creation_ticks": llama_identity.creation_ticks,
                    "gateway_executable": gateway_identity.executable,
                    "gateway_creation_ticks": gateway_identity.creation_ticks,
                    "backend": self.settings.backend,
                    "model": self.settings.model_id,
                },
            )
        except BaseException:
            self.stop()
            raise
        return self.processes

    def stop(self) -> None:
        """Stop only child processes owned by this manager."""

        logger.info("正在停止受管理引擎")
        with self._operation_lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        processes = self.processes
        if processes is None:
            return
        _terminate_process(processes.gateway)
        _terminate_process(processes.llama)
        processes.gateway_log.close()
        processes.llama_log.close()
        self.processes = None
        (self.runtime_root / "managed-processes.json").unlink(missing_ok=True)

    def restart(self) -> ManagedProcesses:
        with self._operation_lock:
            self._stop_locked()
            return self._start_locked()

    def cancel_and_restart(self) -> None:
        """Terminate the managed process tree and wait for a fresh ready engine."""

        with self._operation_lock:
            self._stop_locked()
            self._start_locked()

    def _children_alive(self) -> bool:
        assert self.processes is not None
        return self.processes.llama.poll() is None and self.processes.gateway.poll() is None


def _tool_path(runtime: Path, name: str) -> Path:
    executable = f"{name}.exe" if os.name == "nt" else name
    candidates = (
        runtime / "ffmpeg" / executable,
        runtime / "engine" / executable,
        runtime / "llama-cuda" / executable,
        runtime / "llama-vulkan" / executable,
        runtime / "llama-cpu" / executable,
    )
    return next((item for item in candidates if item.is_file()), candidates[0])


def _wait_http(
    url: str,
    *,
    timeout: float,
    process: subprocess.Popen[bytes] | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    last_error = "尚未回應"
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise EngineError(f"llama-server 在啟動期間結束（exit {process.returncode}）。")
        try:
            response = httpx.get(url, timeout=2.0)
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise EngineError(f"llama-server 啟動逾時：{last_error}")


def _wait_gateway(
    client: QwenGatewayClient,
    *,
    timeout: float,
    processes: tuple[subprocess.Popen[bytes], ...] = (),
) -> None:
    deadline = time.monotonic() + timeout
    last_detail = ""
    while time.monotonic() < deadline:
        exited = [process for process in processes if process.poll() is not None]
        if exited:
            detail = "、".join(f"PID {process.pid} exit {process.returncode}" for process in exited)
            raise EngineError(f"本機辨識引擎在啟動期間結束：{detail}。")
        status = client.health()
        if status.live and status.ready:
            return
        last_detail = status.detail
        time.sleep(0.5)
    raise EngineError(f"Qwen3-ASR Gateway 啟動逾時：{last_detail}")


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
        process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _recover_stale_runtime(paths: EnginePaths) -> None:
    manifest = paths.runtime / "managed-processes.json"
    if not manifest.is_file():
        return
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        owner_pid = int(payload["owner_pid"])
        owner_expected = ProcessIdentity(
            executable=str(payload["owner_executable"]),
            creation_ticks=int(payload["owner_creation_ticks"]),
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise EngineError(f"本機引擎程序紀錄損毀，請刪除後重試：{manifest}") from exc
    owner_actual = _process_identity(owner_pid)
    if (
        owner_pid != os.getpid()
        and owner_actual is not None
        and _same_process(owner_actual, owner_expected)
    ):
        raise EngineError("另一個 HearFlow 程式仍在管理本機引擎；請先關閉另一個視窗。")
    for pid_key, executable_key, creation_key in (
        ("gateway_pid", "gateway_executable", "gateway_creation_ticks"),
        ("llama_pid", "llama_executable", "llama_creation_ticks"),
    ):
        try:
            pid = int(payload[pid_key])
            expected = ProcessIdentity(
                executable=str(payload[executable_key]),
                creation_ticks=int(payload[creation_key]),
            )
        except (ValueError, TypeError, KeyError):
            continue
        actual = _process_identity(pid)
        if actual is not None and _same_process(actual, expected):
            _terminate_pid_tree(pid)
    manifest.unlink(missing_ok=True)


def _process_identity(pid: int) -> ProcessIdentity | None:
    if pid <= 0:
        return None
    if os.name != "nt":
        try:
            executable = os.readlink(f"/proc/{pid}/exe")
            creation_ticks = int(os.stat(f"/proc/{pid}").st_ctime_ns)
        except OSError:
            return None
        return ProcessIdentity(executable, creation_ticks)

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        capacity = wintypes.DWORD(32_768)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            buffer,
            ctypes.byref(capacity),
        ):
            return None
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        creation_ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return ProcessIdentity(buffer.value, creation_ticks)
    finally:
        kernel32.CloseHandle(handle)


def _same_process(actual: ProcessIdentity, expected: ProcessIdentity) -> bool:
    return actual.creation_ticks == expected.creation_ticks and os.path.normcase(
        os.path.abspath(actual.executable)
    ) == os.path.normcase(os.path.abspath(expected.executable))


def _terminate_pid_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
    else:
        with suppress(OSError):
            os.kill(pid, signal.SIGTERM)
