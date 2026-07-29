from __future__ import annotations

import json
from pathlib import Path

import httpx

from hearflow.services.remote_audio import (
    RemoteSpeechSynthesizer,
    _parse_transcription_payload,
)
from hearflow.services.settings import SecretStore, SpeechSettings


class MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


def _store(credential: str = "xai") -> SecretStore:
    backend = MemoryKeyring()
    store = SecretStore(backend)
    store.save_provider_key(credential, "test-key")
    return store


def test_word_timestamps_are_grouped_and_preserve_speakers() -> None:
    result = _parse_transcription_payload(
        {
            "text": "Hello world. Second speaker.",
            "language": "en",
            "words": [
                {"word": "Hello", "start": 0.0, "end": 0.4, "speaker": "A"},
                {"word": "world.", "start": 0.4, "end": 0.9, "speaker": "A"},
                {"word": "Second", "start": 1.0, "end": 1.4, "speaker": "B"},
                {"word": "speaker.", "start": 1.4, "end": 2.0, "speaker": "B"},
            ],
        },
        response_headers={},
        model=None,
        fallback_language="und",
        fallback_duration_seconds=2.0,
    )
    assert len(result.segments) == 2
    assert result.segments[0].metadata["speaker"] == "A"
    assert result.segments[1].metadata["speaker"] == "B"
    assert result.timestamp_accuracy == "chunk"


def test_text_only_response_uses_media_duration_and_marks_estimated() -> None:
    result = _parse_transcription_payload(
        {"text": "第一句。第二句。", "language": "zh"},
        response_headers={},
        model="remote-model",
        fallback_language="und",
        fallback_duration_seconds=12.0,
    )
    assert result.duration >= 12.0
    assert result.segments[-1].end_ms == 12_000
    assert result.timestamp_accuracy == "estimated"
    assert all(item.metadata["timestamp_accuracy"] == "estimated" for item in result.segments)


def test_xai_tts_request_uses_native_endpoint_and_bcp47_language(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"RIFF-test-wave")

    settings = SpeechSettings(
        enabled=True,
        mode="remote",
        provider_id="xai",
        credential_id="xai",
        model="",
        voice="eve",
        language="zh-TW",
        output_format="wav",
        speed=1.0,
    )
    synthesizer = RemoteSpeechSynthesizer(
        settings,
        _store(),
        transport=httpx.MockTransport(handler),
    )
    destination = synthesizer.synthesize("測試", tmp_path / "speech.wav")
    assert destination.read_bytes() == b"RIFF-test-wave"
    assert captured["path"] == "/v1/tts"
    assert captured["authorization"] == "Bearer test-key"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["language"] == "zh-TW"
    assert body["output_format"]["codec"] == "wav"


def test_openrouter_tts_rejects_unsupported_wav_format() -> None:
    settings = SpeechSettings(
        enabled=True,
        mode="remote",
        provider_id="openrouter",
        credential_id="openrouter",
        model="openai/gpt-4o-mini-tts-2025-12-15",
        voice="alloy",
        language="auto",
        output_format="wav",
    )
    with __import__("pytest").raises(ValueError, match="只支援"):
        RemoteSpeechSynthesizer(settings, _store("openrouter"))


def test_openai_stt_uses_multipart_audio_endpoint(tmp_path: Path) -> None:
    from hearflow.services.gateway import TranscriptionOptions
    from hearflow.services.remote_audio import RemoteTranscriptionClient
    from hearflow.services.settings import RemoteTranscriptionSettings

    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"RIFF-fake")
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["content_type"] = request.headers.get("content-type", "")
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"text": "hello", "language": "en"})

    settings = RemoteTranscriptionSettings(
        enabled=True,
        provider_id="openai",
        credential_id="openai",
        model="gpt-4o-mini-transcribe",
    )
    client = RemoteTranscriptionClient(
        settings,
        _store("openai"),
        transport=httpx.MockTransport(handler),
    )
    result = client.transcribe(audio, TranscriptionOptions(duration_seconds=3.5))
    assert captured["path"] == "/v1/audio/transcriptions"
    assert str(captured["content_type"]).startswith("multipart/form-data;")
    assert captured["authorization"] == "Bearer test-key"
    assert result.text == "hello"
    assert result.duration >= 3.5


def test_openrouter_stt_uses_base64_json(tmp_path: Path) -> None:
    from hearflow.services.remote_audio import RemoteTranscriptionClient
    from hearflow.services.settings import RemoteTranscriptionSettings

    audio = tmp_path / "sample.mp3"
    audio.write_bytes(b"fake-mp3")
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        captured["title"] = request.headers.get("x-title")
        return httpx.Response(200, json={"text": "router text", "language": "en"})

    settings = RemoteTranscriptionSettings(
        enabled=True,
        provider_id="openrouter",
        credential_id="openrouter",
        model="openai/gpt-4o-mini-transcribe",
        app_title="HearFlow Tests",
    )
    client = RemoteTranscriptionClient(
        settings,
        _store("openrouter"),
        transport=httpx.MockTransport(handler),
    )
    result = client.transcribe(audio)
    assert captured["path"] == "/api/v1/audio/transcriptions"
    assert captured["title"] == "HearFlow Tests"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["input_audio"]["format"] == "mp3"
    assert body["input_audio"]["data"]
    assert result.text == "router text"


def test_gemini_stt_uses_native_inline_audio(tmp_path: Path) -> None:
    from hearflow.services.remote_audio import RemoteTranscriptionClient
    from hearflow.services.settings import RemoteTranscriptionSettings

    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"fake-wave")
    captured: dict[str, object] = {}
    inner = {
        "text": "Gemini transcript",
        "language": "en",
        "duration": 1.25,
        "segments": [{"start_ms": 0, "end_ms": 1250, "text": "Gemini transcript"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["key"] = request.headers.get("x-goog-api-key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": json.dumps(inner)}]}}
                ]
            },
        )

    settings = RemoteTranscriptionSettings(
        enabled=True,
        provider_id="gemini",
        credential_id="gemini",
        model="gemini-3.6-flash",
    )
    client = RemoteTranscriptionClient(
        settings,
        _store("gemini"),
        transport=httpx.MockTransport(handler),
    )
    result = client.transcribe(audio)
    assert captured["path"].endswith("/models/gemini-3.6-flash:generateContent")
    assert captured["key"] == "test-key"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["contents"][0]["parts"][0]["inlineData"]["data"]
    assert result.segments[0].end_ms == 1250


def test_openrouter_tts_uses_audio_speech_and_provider_instructions(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"mp3-data")

    settings = SpeechSettings(
        enabled=True,
        mode="remote",
        provider_id="openrouter",
        credential_id="openrouter",
        model="openai/gpt-4o-mini-tts-2025-12-15",
        voice="alloy",
        language="auto",
        output_format="mp3",
        instructions="Speak calmly",
    )
    synth = RemoteSpeechSynthesizer(
        settings,
        _store("openrouter"),
        transport=httpx.MockTransport(handler),
    )
    output = synth.synthesize("hello", tmp_path / "speech.mp3")
    assert output.read_bytes() == b"mp3-data"
    assert captured["path"] == "/api/v1/audio/speech"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["provider"]["options"]["openai"]["instructions"] == "Speak calmly"


def test_gemini_tts_wraps_pcm_as_wav(tmp_path: Path) -> None:
    import base64

    raw_pcm = b"\x00\x00\x01\x00" * 20

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "audio/pcm;rate=24000",
                                        "data": base64.b64encode(raw_pcm).decode("ascii"),
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    settings = SpeechSettings(
        enabled=True,
        mode="remote",
        provider_id="gemini",
        credential_id="gemini",
        model="gemini-3.1-flash-tts-preview",
        voice="Kore",
        language="auto",
        output_format="wav",
    )
    synth = RemoteSpeechSynthesizer(
        settings,
        _store("gemini"),
        transport=httpx.MockTransport(handler),
    )
    output = synth.synthesize("hello", tmp_path / "speech.wav")
    data = output.read_bytes()
    assert data.startswith(b"RIFF")
    assert b"WAVE" in data[:16]
    assert data.endswith(raw_pcm)
