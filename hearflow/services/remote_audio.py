"""Remote speech-to-text and text-to-speech provider adapters.

Supported wire protocols:

* OpenAI multipart transcription and Audio Speech
* OpenRouter base64 JSON transcription and OpenAI-compatible Audio Speech
* xAI ``/v1/stt`` and ``/v1/tts``
* Gemini native audio understanding and speech generation

API keys are retrieved from :class:`~hearflow.services.settings.SecretStore` and
never persisted in normal application settings.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import struct
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from hearflow.domain.models import AsrResult, EngineStatus, SubtitleSegment
from hearflow.services.gateway import (
    CancellationToken,
    RequestCancelled,
    TranscriptionOptions,
    request_with_cancellation,
    run_async,
)
from hearflow.services.provider_catalog import (
    AuthStyle,
    ProviderCapability,
    SttApiStyle,
    TtsApiStyle,
    get_provider_profile,
    normalized_base_url,
)
from hearflow.services.settings import RemoteTranscriptionSettings, SecretStore, SpeechSettings

logger = logging.getLogger(__name__)


class RemoteAudioError(RuntimeError):
    """Raised when a remote ASR or TTS request cannot be completed safely."""


class RemoteTranscriptionClient:
    """Transcribe media through a configured remote API provider."""

    def __init__(
        self,
        settings: RemoteTranscriptionSettings,
        secret_store: SecretStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.settings = settings
        self.secret_store = secret_store
        self.transport = transport
        self.timeout = timeout
        self.profile = get_provider_profile(settings.provider_id)
        if not self.profile.supports(ProviderCapability.TRANSCRIPTION):
            raise ValueError(f"{self.profile.label} 沒有公開的語音轉錄能力。")

    def health(self) -> EngineStatus:
        """Return a non-destructive provider connectivity check."""

        return run_async(self.ahealth())

    async def ahealth(self) -> EngineStatus:
        key = self._api_key(required=not _is_loopback(self._base_url()))
        headers = _auth_headers(self.profile.audio_auth, key)
        try:
            if self.profile.stt_style is SttApiStyle.GEMINI_NATIVE:
                url = f"{self._base_url()}/models"
                response = await self._request("GET", url, headers=headers, timeout=15.0)
            elif self.profile.id == "xai":
                # xAI exposes its model catalogue through the common /models route.
                url = f"{self._base_url()}/models"
                response = await self._request("GET", url, headers=headers, timeout=15.0)
            else:
                url = f"{self._base_url()}/models"
                response = await self._request("GET", url, headers=headers, timeout=15.0)
            if response.status_code in {200, 401, 403}:
                ready = response.status_code == 200
                detail = "遠端語音 API 可連線。" if ready else "遠端 API 可連線，但金鑰未通過驗證。"
                return EngineStatus(
                    live=True,
                    ready=ready,
                    models=((self.settings.model,) if self.settings.model else ()),
                    detail=detail,
                    http_status=response.status_code,
                )
            return EngineStatus(
                live=False,
                ready=False,
                detail=f"遠端語音 API 回傳 HTTP {response.status_code}。",
                http_status=response.status_code,
            )
        except (httpx.HTTPError, OSError, RemoteAudioError) as exc:
            return EngineStatus(live=False, ready=False, detail=str(exc))

    def transcribe(
        self,
        media_path: str | Path,
        options: TranscriptionOptions | None = None,
        cancellation: CancellationToken | None = None,
    ) -> AsrResult:
        """Transcribe one file using the same interface as QwenGatewayClient."""

        return run_async(self.atranscribe(media_path, options, cancellation))

    async def atranscribe(
        self,
        media_path: str | Path,
        options: TranscriptionOptions | None = None,
        cancellation: CancellationToken | None = None,
    ) -> AsrResult:
        path = Path(media_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise RemoteAudioError(f"找不到媒體檔案：{path}")
        size = path.stat().st_size
        if size > self.settings.max_file_bytes:
            raise RemoteAudioError(
                f"媒體檔案為 {size / 1024 / 1024:.1f} MiB，超過目前遠端上傳上限 "
                f"{self.settings.max_file_bytes / 1024 / 1024:.1f} MiB；請先切段。"
            )

        parsed_options = options or TranscriptionOptions()
        token = cancellation or CancellationToken()
        token.raise_if_cancelled()
        requested_model = parsed_options.model.strip()
        if requested_model in {"", "qwen3-asr", "qwen3-asr-0.6b"}:
            requested_model = ""
        selected_model = (
            requested_model or self.settings.model or self.profile.default_stt_model
        ).strip()
        selected_language = (
            parsed_options.language
            if parsed_options.language not in {"", "auto"}
            else self.settings.language
        )
        prompt = parsed_options.prompt.strip() or self.settings.prompt
        temperature = (
            parsed_options.temperature
            if parsed_options.temperature > 0
            else self.settings.temperature
        )
        response_format = self.settings.response_format
        started = time.perf_counter()

        try:
            style = self.profile.stt_style
            if style is SttApiStyle.OPENAI_MULTIPART:
                payload, headers = await self._openai_transcription(
                    path,
                    model=selected_model,
                    language=selected_language,
                    prompt=prompt,
                    temperature=temperature,
                    response_format=response_format,
                    cancellation=token,
                )
            elif style is SttApiStyle.OPENROUTER_JSON:
                payload, headers = await self._openrouter_transcription(
                    path,
                    model=selected_model,
                    language=selected_language,
                    temperature=temperature,
                    response_format=response_format,
                    cancellation=token,
                )
            elif style is SttApiStyle.XAI_MULTIPART:
                payload, headers = await self._xai_transcription(
                    path,
                    language=selected_language,
                    prompt=prompt,
                    response_format=response_format,
                    cancellation=token,
                )
            elif style is SttApiStyle.GEMINI_NATIVE:
                payload, headers = await self._gemini_transcription(
                    path,
                    model=selected_model,
                    language=selected_language,
                    prompt=prompt,
                    temperature=temperature,
                    cancellation=token,
                )
            else:
                raise RemoteAudioError(f"{self.profile.label} 沒有可用的 STT 請求格式。")
        except RequestCancelled:
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RemoteAudioError(f"遠端語音轉錄失敗：{exc}") from exc

        result = _parse_transcription_payload(
            payload,
            response_headers=headers,
            model=selected_model or None,
            fallback_language=(selected_language if selected_language != "auto" else "und"),
            fallback_duration_seconds=parsed_options.duration_seconds,
        )
        result.elapsed_seconds = time.perf_counter() - started
        return result

    async def _openai_transcription(
        self,
        path: Path,
        *,
        model: str,
        language: str,
        prompt: str,
        temperature: float,
        response_format: str,
        cancellation: CancellationToken,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        if not model:
            raise RemoteAudioError("遠端語音轉錄模型不可留空。")
        key = self._api_key(required=not _is_loopback(self._base_url()))
        effective_format = response_format or "json"
        if model.startswith(("gpt-4o-transcribe", "gpt-4o-mini-transcribe")):
            effective_format = "json"
        elif model == "gpt-4o-transcribe-diarize":
            effective_format = "diarized_json"
        data: dict[str, str] = {
            "model": model,
            "response_format": effective_format,
        }
        if language and language != "auto":
            data["language"] = language
        if prompt:
            data["prompt"] = prompt
        if temperature > 0:
            data["temperature"] = str(temperature)
        if data["response_format"] == "verbose_json":
            data["timestamp_granularities[]"] = "segment"
        with path.open("rb") as stream:
            response = await _cancellable_request(
                "POST",
                f"{self._base_url()}/audio/transcriptions",
                cancellation=cancellation,
                headers=_auth_headers(self.profile.audio_auth, key),
                data=data,
                files={"file": (path.name, stream, _mime_type(path))},
                timeout=self.timeout,
                transport=self.transport,
            )
        payload = _json_response(response, "OpenAI-compatible STT")
        return payload, dict(response.headers)

    async def _openrouter_transcription(
        self,
        path: Path,
        *,
        model: str,
        language: str,
        temperature: float,
        response_format: str,
        cancellation: CancellationToken,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        if not model:
            raise RemoteAudioError("OpenRouter STT 模型不可留空。")
        key = self._api_key(required=True)
        body: dict[str, Any] = {
            "model": model,
            "input_audio": {
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                "format": _audio_format(path),
            },
        }
        if language and language != "auto":
            body["language"] = language
        if temperature > 0:
            body["temperature"] = temperature
        effective_format = (
            response_format if response_format in {"json", "verbose_json"} else "json"
        )
        body["response_format"] = effective_format
        if effective_format == "verbose_json":
            body["timestamp_granularities"] = ["segment"]
        response = await _cancellable_request(
            "POST",
            f"{self._base_url()}/audio/transcriptions",
            cancellation=cancellation,
            headers={
                **_auth_headers(self.profile.audio_auth, key),
                "Content-Type": "application/json",
                **_openrouter_attribution_headers(
                    self.settings.http_referer, self.settings.app_title
                ),
            },
            json=body,
            timeout=self.timeout,
            transport=self.transport,
        )
        payload = _json_response(response, "OpenRouter STT")
        return payload, dict(response.headers)

    async def _xai_transcription(
        self,
        path: Path,
        *,
        language: str,
        prompt: str,
        response_format: str,
        cancellation: CancellationToken,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        key = self._api_key(required=True)
        data: list[tuple[str, str]] = [("format", "true")]
        if response_format == "diarized_json":
            data.append(("diarize", "true"))
        if language and language != "auto":
            data.append(("language", language))
        if prompt:
            for term in _split_keyterms(prompt):
                data.append(("keyterm", term))
        with path.open("rb") as stream:
            response = await _cancellable_request(
                "POST",
                f"{self._base_url()}/stt",
                cancellation=cancellation,
                headers=_auth_headers(self.profile.audio_auth, key),
                data=data,
                files={"file": (path.name, stream, _mime_type(path))},
                timeout=self.timeout,
                transport=self.transport,
            )
        payload = _json_response(response, "xAI STT")
        return payload, dict(response.headers)

    async def _gemini_transcription(
        self,
        path: Path,
        *,
        model: str,
        language: str,
        prompt: str,
        temperature: float,
        cancellation: CancellationToken,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        if not model:
            raise RemoteAudioError("Gemini 音訊理解模型不可留空。")
        key = self._api_key(required=True)
        language_note = "自動偵測語言" if language in {"", "auto"} else f"語言提示：{language}"
        request_prompt = (
            "請完整轉錄音訊，保留原語言與標點。回傳 JSON object，格式為："
            '{"text":"完整逐字稿","language":"BCP-47 或 ISO 代碼",'
            '"duration":秒數,"segments":[{"start_ms":0,"end_ms":1000,"text":"..."}]}。'
            "時間只能使用你能從音訊判斷的片段級時間，不得捏造逐字精度。"
            f"\n{language_note}"
        )
        if prompt:
            request_prompt += f"\n專有名詞與上下文：{prompt}"
        schema = {
            "type": "OBJECT",
            "properties": {
                "text": {"type": "STRING"},
                "language": {"type": "STRING"},
                "duration": {"type": "NUMBER"},
                "segments": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "start_ms": {"type": "INTEGER"},
                            "end_ms": {"type": "INTEGER"},
                            "text": {"type": "STRING"},
                        },
                        "required": ["start_ms", "end_ms", "text"],
                    },
                },
            },
            "required": ["text", "language", "segments"],
        }
        body = {
            "contents": [
                {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": _mime_type(path),
                                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                            }
                        },
                        {"text": request_prompt},
                    ]
                }
            ],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }
        response = await _cancellable_request(
            "POST",
            f"{self._base_url()}/models/{model}:generateContent",
            cancellation=cancellation,
            headers={
                **_auth_headers(self.profile.audio_auth, key),
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout,
            transport=self.transport,
        )
        outer = _json_response(response, "Gemini audio understanding")
        text = _gemini_text(outer)
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise RemoteAudioError("Gemini 轉錄回應不是 JSON object。")
        return parsed, dict(response.headers)

    def _base_url(self) -> str:
        return normalized_base_url(
            self.profile,
            self.settings.base_url,
            capability=ProviderCapability.TRANSCRIPTION,
        )

    def _api_key(self, *, required: bool) -> str | None:
        value = self.secret_store.load_provider_key(self.settings.credential_id)
        if required and not value:
            raise RemoteAudioError(
                f"找不到 {self.profile.label} 金鑰；請在偏好設定儲存 credential "
                f"{self.settings.credential_id!r}。"
            )
        return value

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(transport=self.transport, follow_redirects=False) as client:
            return await client.request(method, url, **kwargs)


class RemoteSpeechSynthesizer:
    """Generate speech through OpenAI, OpenRouter, xAI, or Gemini."""

    def __init__(
        self,
        settings: SpeechSettings,
        secret_store: SecretStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.settings = settings
        self.secret_store = secret_store
        self.transport = transport
        self.timeout = timeout
        self.profile = get_provider_profile(settings.provider_id)
        if not self.profile.supports(ProviderCapability.SPEECH):
            raise ValueError(f"{self.profile.label} 沒有公開的 TTS 能力。")
        supported_formats = self.profile.supported_tts_formats
        if supported_formats and settings.output_format not in supported_formats:
            allowed = "、".join(supported_formats)
            raise ValueError(f"{self.profile.label} TTS 只支援：{allowed}。")
        if self.profile.id == "xai" and not 0.7 <= settings.speed <= 1.5:
            raise ValueError("xAI TTS speed 必須介於 0.7 與 1.5。")

    @property
    def output_extension(self) -> str:
        return self.settings.output_format

    def synthesize(
        self,
        text: str,
        output_path: str | Path,
        *,
        language: str | None = None,
        voice: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> Path:
        return run_async(
            self.asynthesize(
                text,
                output_path,
                language=language,
                voice=voice,
                cancellation=cancellation,
            )
        )

    async def asynthesize(
        self,
        text: str,
        output_path: str | Path,
        *,
        language: str | None = None,
        voice: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> Path:
        normalized = text.strip()
        if not normalized:
            raise RemoteAudioError("TTS 文字不可留空。")
        destination = Path(output_path).expanduser().resolve(strict=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        token = cancellation or CancellationToken()
        token.raise_if_cancelled()
        selected_voice = (voice or self.settings.voice).strip()
        selected_language = (language or self.settings.language).strip()
        selected_model = (self.settings.model or self.profile.default_tts_model).strip()
        key = self.secret_store.load_provider_key(self.settings.credential_id)
        if not key and not _is_loopback(self._base_url()):
            raise RemoteAudioError(f"找不到 {self.profile.label} TTS API 金鑰。")

        try:
            if self.profile.tts_style is TtsApiStyle.OPENAI_SPEECH:
                content = await self._openai_speech(
                    normalized,
                    model=selected_model,
                    voice=selected_voice,
                    key=key,
                    cancellation=token,
                )
            elif self.profile.tts_style is TtsApiStyle.XAI_TTS:
                content = await self._xai_speech(
                    normalized,
                    voice=selected_voice,
                    language=selected_language,
                    key=key,
                    cancellation=token,
                )
            elif self.profile.tts_style is TtsApiStyle.GEMINI_NATIVE:
                content = await self._gemini_speech(
                    normalized,
                    model=selected_model,
                    voice=selected_voice,
                    key=key,
                    cancellation=token,
                )
            else:
                raise RemoteAudioError(f"{self.profile.label} 沒有可用的 TTS 請求格式。")
        except RequestCancelled:
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RemoteAudioError(f"遠端 TTS 失敗：{exc}") from exc

        partial = destination.with_suffix(destination.suffix + ".partial")
        partial.write_bytes(content)
        partial.replace(destination)
        return destination

    async def _openai_speech(
        self,
        text: str,
        *,
        model: str,
        voice: str,
        key: str | None,
        cancellation: CancellationToken,
    ) -> bytes:
        if not model:
            raise RemoteAudioError("TTS 模型不可留空。")
        body: dict[str, Any] = {
            "model": model,
            "input": text,
            "voice": voice or "alloy",
            "response_format": self.settings.output_format,
            "speed": self.settings.speed,
        }
        if self.settings.instructions:
            if self.profile.id == "openrouter":
                body["provider"] = {
                    "options": {"openai": {"instructions": self.settings.instructions}}
                }
            else:
                body["instructions"] = self.settings.instructions
        response = await _cancellable_request(
            "POST",
            f"{self._base_url()}/audio/speech",
            cancellation=cancellation,
            headers={
                **_auth_headers(self.profile.audio_auth, key),
                "Content-Type": "application/json",
                **_openrouter_attribution_headers(
                    self.settings.http_referer, self.settings.app_title
                ),
            },
            json=body,
            timeout=self.timeout,
            transport=self.transport,
        )
        _raise_audio_error(response, f"{self.profile.label} TTS")
        return response.content

    async def _xai_speech(
        self,
        text: str,
        *,
        voice: str,
        language: str,
        key: str | None,
        cancellation: CancellationToken,
    ) -> bytes:
        codec = self.settings.output_format
        if codec not in {"mp3", "wav", "pcm"}:
            raise RemoteAudioError("xAI TTS output_format 必須是 mp3、wav 或 pcm。")
        output_format: dict[str, Any] = {
            "codec": codec,
            "sample_rate": 24_000,
        }
        if codec == "mp3":
            output_format["bit_rate"] = 128_000
        body: dict[str, Any] = {
            "text": text,
            "voice_id": voice or "eve",
            "language": _normalize_xai_language(language),
            "speed": self.settings.speed,
            "output_format": output_format,
        }
        response = await _cancellable_request(
            "POST",
            f"{self._base_url()}/tts",
            cancellation=cancellation,
            headers={
                **_auth_headers(self.profile.audio_auth, key),
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout,
            transport=self.transport,
        )
        _raise_audio_error(response, "xAI TTS")
        return response.content

    async def _gemini_speech(
        self,
        text: str,
        *,
        model: str,
        voice: str,
        key: str | None,
        cancellation: CancellationToken,
    ) -> bytes:
        if not model:
            raise RemoteAudioError("Gemini TTS 模型不可留空。")
        prompt = text
        if self.settings.instructions:
            prompt = f"{self.settings.instructions}\n\nTRANSCRIPT:\n{text}"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice or "Kore"}}
                },
            },
        }
        response = await _cancellable_request(
            "POST",
            f"{self._base_url()}/models/{model}:generateContent",
            cancellation=cancellation,
            headers={
                **_auth_headers(self.profile.audio_auth, key),
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout,
            transport=self.transport,
        )
        payload = _json_response(response, "Gemini TTS")
        try:
            parts = payload["candidates"][0]["content"]["parts"]
            audio_part = next(
                part
                for part in parts
                if isinstance(part, Mapping) and ("inlineData" in part or "inline_data" in part)
            )
            inline = audio_part.get("inlineData") or audio_part["inline_data"]
            raw = base64.b64decode(str(inline["data"]), validate=True)
            mime = str(inline.get("mimeType") or inline.get("mime_type") or "audio/pcm")
        except (KeyError, IndexError, StopIteration, TypeError, ValueError) as exc:
            raise RemoteAudioError("Gemini TTS 回應缺少音訊資料。") from exc
        if self.settings.output_format == "wav" and "pcm" in mime:
            return _pcm16le_to_wav(raw, sample_rate=24_000)
        return raw

    def _base_url(self) -> str:
        return normalized_base_url(
            self.profile,
            self.settings.base_url,
            capability=ProviderCapability.SPEECH,
        )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _cancellable_request(
    method: str,
    url: str,
    *,
    cancellation: CancellationToken,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: httpx.Timeout | float = 300.0,
    **kwargs: Any,
) -> httpx.Response:
    async with httpx.AsyncClient(
        timeout=timeout,
        transport=transport,
        follow_redirects=False,
    ) as client:
        return await request_with_cancellation(
            client,
            method,
            url,
            cancellation=cancellation,
            **kwargs,
        )


def _auth_headers(style: AuthStyle, api_key: str | None) -> dict[str, str]:
    if not api_key or style is AuthStyle.NONE:
        return {}
    if style is AuthStyle.BEARER:
        return {"Authorization": f"Bearer {api_key}"}
    if style is AuthStyle.GOOGLE_API_KEY:
        return {"x-goog-api-key": api_key}
    raise ValueError(f"不支援的認證格式：{style}")


def _openrouter_attribution_headers(http_referer: str, app_title: str) -> dict[str, str]:
    result: dict[str, str] = {}
    if http_referer.strip():
        result["HTTP-Referer"] = http_referer.strip()
    if app_title.strip():
        result["X-Title"] = app_title.strip()
    return result


def _json_response(response: httpx.Response, label: str) -> dict[str, Any]:
    if response.status_code >= 400:
        detail = response.text.strip()[:1200]
        raise RemoteAudioError(f"{label} 回傳 HTTP {response.status_code}：{detail}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RemoteAudioError(f"{label} 回傳的內容不是 JSON。") from exc
    if not isinstance(payload, dict):
        raise RemoteAudioError(f"{label} 回傳的 JSON 不是 object。")
    return payload


def _raise_audio_error(response: httpx.Response, label: str) -> None:
    if response.status_code < 400:
        if not response.content:
            raise RemoteAudioError(f"{label} 回傳空白音訊。")
        return
    detail = response.text.strip()[:1200]
    raise RemoteAudioError(f"{label} 回傳 HTTP {response.status_code}：{detail}")


def _parse_transcription_payload(
    payload: Mapping[str, Any],
    *,
    response_headers: Mapping[str, str],
    model: str | None,
    fallback_language: str,
    fallback_duration_seconds: float = 0.0,
) -> AsrResult:
    text = str(payload.get("text") or "").strip()
    language = str(payload.get("language") or fallback_language or "und")
    duration = max(
        _safe_float(payload.get("duration"), 0.0),
        _usage_duration(payload.get("usage")),
        _safe_float(fallback_duration_seconds, 0.0),
    )
    raw_segments = payload.get("segments")
    timestamp_accuracy = "chunk"
    if not isinstance(raw_segments, list):
        raw_segments = _segments_from_words(payload.get("words"))
    if not raw_segments and text:
        raw_segments = _estimate_segments(text, duration)
        timestamp_accuracy = "estimated"
    segments: list[SubtitleSegment] = []
    if isinstance(raw_segments, list):
        for index, item in enumerate(raw_segments, start=1):
            if not isinstance(item, Mapping):
                continue
            item_text = str(item.get("text") or "").strip()
            if not item_text:
                continue
            start_ms = _time_ms(item, "start_ms", "start")
            end_ms = _time_ms(item, "end_ms", "end")
            if end_ms <= start_ms:
                end_ms = start_ms + 1
            segments.append(
                SubtitleSegment(
                    id=f"remote-{index:05d}",
                    start_ms=start_ms,
                    end_ms=end_ms,
                    source_text=item_text,
                    status="raw",
                    metadata={
                        "timestamp_accuracy": str(
                            item.get("timestamp_accuracy") or timestamp_accuracy
                        ),
                        "provider_model": model,
                        **(
                            {"speaker": str(item["speaker"])}
                            if item.get("speaker") is not None
                            else {}
                        ),
                    },
                )
            )
            duration = max(duration, end_ms / 1000.0)

    if not text and segments:
        text = " ".join(segment.source_text for segment in segments)
    if not text:
        raise RemoteAudioError("遠端 STT 回應沒有逐字稿文字。")
    if not segments:
        end_ms = max(1000, int(round(duration * 1000)))
        duration = max(duration, end_ms / 1000.0)
        segments = [
            SubtitleSegment(
                id="remote-00001",
                start_ms=0,
                end_ms=end_ms,
                source_text=text,
                status="raw",
                metadata={"timestamp_accuracy": "estimated", "provider_model": model},
            )
        ]

    return AsrResult(
        raw_response=dict(payload),
        response_headers=dict(response_headers),
        text=text,
        language=language,
        duration=duration,
        segments=segments,
        model=model,
        chunk_count=max(1, len(segments)),
        timestamp_accuracy=(
            "chunk"
            if all(item.metadata.get("timestamp_accuracy") == "chunk" for item in segments)
            else "estimated"
        ),
    )


def _usage_duration(value: Any) -> float:
    if not isinstance(value, Mapping):
        return 0.0
    return max(
        _safe_float(value.get("seconds"), 0.0),
        _safe_float(value.get("duration"), 0.0),
        _safe_float(value.get("audio_seconds"), 0.0),
    )


def _estimate_segments(text: str, duration: float) -> list[dict[str, Any]]:
    """Create explicitly estimated subtitle timing when a provider returns text only."""

    normalized = re.sub(r"[ \t]+", " ", text).strip()
    if not normalized:
        return []
    sentence_parts = [
        item.strip() for item in re.split(r"(?<=[。！？.!?])\s*|\n+", normalized) if item.strip()
    ]
    pieces: list[str] = []
    for sentence in sentence_parts or [normalized]:
        remaining = sentence
        while len(remaining) > 42:
            boundary = max(
                remaining.rfind(mark, 0, 43) for mark in ("，", ",", "；", ";", "、", " ")
            )
            if boundary < 14:
                boundary = 42
            else:
                boundary += 1
            pieces.append(remaining[:boundary].strip())
            remaining = remaining[boundary:].strip()
        if remaining:
            pieces.append(remaining)
    total_weight = sum(max(1, len(item)) for item in pieces)
    effective_duration = duration if duration > 0 else max(1.0, len(normalized) / 8.0)
    total_ms = max(len(pieces), int(round(effective_duration * 1000)))
    output: list[dict[str, Any]] = []
    cursor = 0
    consumed_weight = 0
    for index, piece in enumerate(pieces):
        consumed_weight += max(1, len(piece))
        end_ms = (
            total_ms
            if index == len(pieces) - 1
            else max(cursor + 1, round(total_ms * consumed_weight / total_weight))
        )
        output.append(
            {
                "start_ms": cursor,
                "end_ms": end_ms,
                "text": piece,
                "timestamp_accuracy": "estimated",
            }
        )
        cursor = end_ms
    return output


def _segments_from_words(value: Any) -> list[dict[str, Any]]:
    """Group word timestamps into honest subtitle-sized chunks."""

    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    current: list[str] = []
    current_speaker: str | None = None
    start: float | None = None
    end = 0.0
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        word = str(raw.get("text") or raw.get("word") or "").strip()
        if not word:
            continue
        word_start = _safe_float(raw.get("start"), end)
        word_end = _safe_float(raw.get("end"), word_start)
        speaker_value = raw.get("speaker")
        speaker = None if speaker_value is None else str(speaker_value)
        if current and speaker != current_speaker:
            result.append(
                {
                    "start": start if start is not None else word_start,
                    "end": max(end, (start or word_start) + 0.001),
                    "text": _join_words(current),
                    "speaker": current_speaker,
                }
            )
            current = []
            start = None
        if start is None:
            start = word_start
            current_speaker = speaker
        current.append(word)
        end = max(word_end, word_start)
        text = _join_words(current)
        sentence_end = word.endswith((".", "!", "?", "。", "！", "？"))
        if sentence_end or len(text) >= 42 or end - start >= 5.5:
            result.append(
                {
                    "start": start,
                    "end": max(end, start + 0.001),
                    "text": text,
                    "speaker": current_speaker,
                }
            )
            current = []
            current_speaker = None
            start = None
    if current and start is not None:
        result.append(
            {
                "start": start,
                "end": max(end, start + 0.001),
                "text": _join_words(current),
                "speaker": current_speaker,
            }
        )
    return result


def _join_words(words: list[str]) -> str:
    text = " ".join(words)
    for mark in (",", ".", "!", "?", ":", ";", "%"):
        text = text.replace(f" {mark}", mark)
    return text.strip()


def _normalize_xai_language(value: str) -> str:
    """Return an xAI-compatible BCP-47 language code."""

    normalized = (value or "auto").strip().replace("_", "-")
    aliases = {
        "": "auto",
        "auto": "auto",
        "automatic": "auto",
        "chinese": "zh",
        "mandarin": "zh",
        "traditional chinese": "zh",
        "simplified chinese": "zh",
        "中文": "zh",
        "繁體中文": "zh",
        "简体中文": "zh",
        "english": "en",
        "英文": "en",
        "japanese": "ja",
        "日文": "ja",
        "korean": "ko",
        "韓文": "ko",
    }
    lowered = normalized.casefold()
    if lowered in aliases:
        return aliases[lowered]
    if len(normalized) == 2:
        return normalized.casefold()
    if "-" in normalized:
        language, region = normalized.split("-", 1)
        return f"{language.casefold()}-{region.upper()}"
    return normalized


def _split_keyterms(prompt: str) -> tuple[str, ...]:
    normalized = prompt.replace("\r", "\n").replace("，", ",")
    terms = []
    for line in normalized.split("\n"):
        for item in line.split(","):
            term = item.strip()
            if term and term not in terms:
                terms.append(term[:50])
            if len(terms) >= 100:
                return tuple(terms)
    return tuple(terms)


def _time_ms(item: Mapping[str, Any], millisecond_key: str, second_key: str) -> int:
    value = item.get(millisecond_key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0, int(round(float(value))))
    value = item.get(second_key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0, int(round(float(value) * 1000)))
    return 0


def _safe_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, parsed)


def _gemini_text(payload: Mapping[str, Any]) -> str:
    try:
        parts = payload["candidates"][0]["content"]["parts"]
        return "".join(
            str(part.get("text", "")) for part in parts if isinstance(part, Mapping)
        ).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RemoteAudioError("Gemini 回應缺少文字內容。") from exc


def _mime_type(path: Path) -> str:
    guessed, _encoding = mimetypes.guess_type(path.name)
    if guessed and (guessed.startswith("audio/") or guessed.startswith("video/")):
        return guessed
    return {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".mp4": "video/mp4",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".webm": "audio/webm",
        ".aac": "audio/aac",
    }.get(path.suffix.casefold(), "application/octet-stream")


def _audio_format(path: Path) -> str:
    suffix = path.suffix.casefold().lstrip(".")
    aliases = {"wave": "wav", "mpeg": "mp3", "mp4a": "m4a"}
    return aliases.get(suffix, suffix or "wav")


def _is_loopback(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").casefold()
    return host in {"127.0.0.1", "::1", "localhost"} or host.endswith(".localhost")


def _pcm16le_to_wav(pcm: bytes, *, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap signed 16-bit little-endian PCM in a minimal WAV container."""

    bits_per_sample = 16
    block_align = channels * bits_per_sample // 8
    byte_rate = sample_rate * block_align
    data_size = len(pcm)
    riff_size = 36 + data_size
    return b"".join(
        [
            b"RIFF",
            struct.pack("<I", riff_size),
            b"WAVEfmt ",
            struct.pack(
                "<IHHIIHH",
                16,
                1,
                channels,
                sample_rate,
                byte_rate,
                block_align,
                bits_per_sample,
            ),
            b"data",
            struct.pack("<I", data_size),
            pcm,
        ]
    )
