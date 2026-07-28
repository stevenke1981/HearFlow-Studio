from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from hearflow.services.gateway import GatewayProtocolError, QwenGatewayClient


def test_health_and_transcription_use_real_http_contract(
    media_file: Path,
) -> None:
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.method,
                request.url.path,
                request.headers.get("authorization"),
            )
        )
        if request.url.path == "/health/live":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/health/ready":
            return httpx.Response(
                200,
                json={
                    "status": "ready",
                    "backend": "llama.cpp",
                    "model": "qwen3-asr-0.6b",
                    "upstream_status": 200,
                },
            )
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"id": "qwen3-asr-0.6b"}]},
            )
        assert request.url.path == "/v1/audio/transcriptions"
        body = request.content
        assert b'name="response_format"' in body
        assert b"verbose_json" in body
        assert media_file.name.encode() in body
        return httpx.Response(
            200,
            headers={
                "x-asr-model": "qwen3-asr-0.6b",
                "x-asr-chunks": "1",
                "x-asr-timestamp-accuracy": "chunk",
            },
            json={
                "text": "真實契約",
                "language": "Chinese",
                "duration": 1.234,
                "segments": [
                    {
                        "id": 0,
                        "seek": 0,
                        "start": 0.0,
                        "end": 1.234,
                        "text": "真實契約",
                    }
                ],
                "task": "transcribe",
                "timestamp_accuracy": "chunk",
            },
        )

    client = QwenGatewayClient(
        "http://gateway.test",
        "secret",
        transport=httpx.MockTransport(handler),
    )
    health = client.health()
    result = client.transcribe(media_file)

    assert health.live and health.ready
    assert health.models == ("qwen3-asr-0.6b",)
    assert result.text == "真實契約"
    assert result.segments[0].end_ms == 1_234
    assert result.timestamp_accuracy == "chunk"
    assert all(auth == "Bearer secret" for _, path, auth in seen if path == "/v1/models")
    assert any(path == "/v1/audio/transcriptions" for _, path, _ in seen)


def test_transcription_rejects_non_chunk_accuracy(media_file: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        payload = {
            "text": "x",
            "language": "English",
            "duration": 1,
            "segments": [{"id": 0, "seek": 0, "start": 0, "end": 1, "text": "x"}],
            "task": "transcribe",
            "timestamp_accuracy": "chunk",
        }
        return httpx.Response(
            200,
            headers={"x-asr-timestamp-accuracy": "word"},
            content=json.dumps(payload).encode(),
        )

    client = QwenGatewayClient(
        "http://gateway.test",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GatewayProtocolError, match="非分片級"):
        client.transcribe(media_file)
