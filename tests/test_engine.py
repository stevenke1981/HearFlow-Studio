from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from hearflow.services.engine import EngineManager
from hearflow.services.settings import EngineSettings

_RUNTIME_FILES = {
    "llama_server": "engine/cpu/llama-server.exe",
    "gateway": "gateway/qwen3-asr-gateway.exe",
    "model": "models/Qwen3-ASR-0.6B-Q8_0.gguf",
    "mmproj": "models/mmproj-Qwen3-ASR-0.6B-Q8_0.gguf",
    "ffmpeg": "ffmpeg/ffmpeg.exe",
    "ffprobe": "ffmpeg/ffprobe.exe",
}


def _runtime_with_manifest(root: Path) -> tuple[EngineManager, dict[str, Path]]:
    paths: dict[str, Path] = {}
    records: dict[str, dict[str, object]] = {}
    for index, (name, relative) in enumerate(_RUNTIME_FILES.items(), start=1):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        content = f"runtime-{index}".encode()
        target.write_bytes(content)
        paths[name] = target
        records[name] = {
            "path": relative,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    manifest = {
        "schema_version": 1,
        "backend": "cpu",
        "files": records,
        "security": {"contains_credentials": False},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    settings = EngineSettings(
        backend="cpu",
        gateway_url="http://127.0.0.1:18000",
        gateway_port=18000,
        llama_port=18080,
    )
    return EngineManager(settings, runtime_root=root), paths


def test_verify_installation_checks_manifest_sizes_and_hashes(tmp_path: Path) -> None:
    manager, paths = _runtime_with_manifest(tmp_path / "runtime")

    verified = manager.verify_installation()
    assert verified["integrity_ready"] is True

    paths["model"].write_bytes(b"x" * paths["model"].stat().st_size)
    corrupted = manager.verify_installation()
    assert corrupted["integrity_ready"] is False
    assert "SHA-256" in corrupted["integrity_detail"]


def test_verify_installation_rejects_manifest_path_escape(tmp_path: Path) -> None:
    manager, _paths = _runtime_with_manifest(tmp_path / "runtime")
    outside = tmp_path / "outside.exe"
    outside.write_bytes(b"outside")
    manifest_path = manager.runtime_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["gateway"] = {
        "path": "../outside.exe",
        "bytes": outside.stat().st_size,
        "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    rejected = manager.verify_installation()
    assert rejected["integrity_ready"] is False
    assert "允許目錄之外" in rejected["integrity_detail"]


def test_manager_serializes_concurrent_start_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = EngineManager(
        EngineSettings(
            backend="cpu",
            gateway_url="http://127.0.0.1:18000",
            gateway_port=18000,
            llama_port=18080,
        ),
        runtime_root=tmp_path,
    )
    barrier = threading.Barrier(3)
    state_lock = threading.Lock()
    state = {"active": 0, "maximum": 0}

    def fake_start() -> object:
        with state_lock:
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
        time.sleep(0.05)
        with state_lock:
            state["active"] -= 1
        return object()

    monkeypatch.setattr(manager, "_start_locked", fake_start)

    def invoke() -> None:
        barrier.wait()
        manager.start()

    threads = [threading.Thread(target=invoke) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert state["maximum"] == 1
