"""Translation providers with strict id-based OpenAI-compatible JSON mapping."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from hearflow.domain.models import SubtitleSegment, TranslationStatus
from hearflow.services.gateway import (
    CancellationToken,
    RequestCancelled,
    request_with_cancellation,
    run_async,
)
from hearflow.services.settings import SecretStore, TranslationSettings

_MARKER_PATTERN = re.compile(
    r"(?:<[^<>\r\n]+>|\{\\[^{}\r\n]+\}|\{\{[^{}\r\n]+\}\}|%\([^)]+\)[a-zA-Z]|%[a-zA-Z])"
)


class TranslationError(RuntimeError):
    """Base class for safe, actionable translation failures."""


class TranslationConfigurationError(TranslationError):
    """Raised before sending an invalid provider configuration."""


class TranslationProtocolError(TranslationError):
    """Raised when a provider response cannot be mapped without data loss."""


class TranslationProviderError(TranslationError):
    """HTTP/provider failure with bounded-retry metadata."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Translation:
    """Outcome for exactly one source segment."""

    id: str
    text: str | None
    status: TranslationStatus
    error: str | None = None
    attempts: int = 0

    @property
    def segment_id(self) -> str:
        return self.id

    @property
    def translated_text(self) -> str | None:
        return self.text


class Translator(Protocol):
    """Common translation provider boundary used by the workflow."""

    def translate(
        self,
        segments: Sequence[SubtitleSegment] | Sequence[Mapping[str, Any]],
        cancellation: CancellationToken | None = None,
    ) -> list[Translation]: ...


class DisabledTranslator:
    """Explicitly skip translation without copying source into target."""

    def translate(
        self,
        segments: Sequence[SubtitleSegment] | Sequence[Mapping[str, Any]],
        cancellation: CancellationToken | None = None,
    ) -> list[Translation]:
        parsed = _parse_source_segments(segments)
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        return [
            Translation(
                id=item.id,
                text=None,
                status=TranslationStatus.SKIPPED,
                attempts=0,
            )
            for item in parsed
        ]


class OpenAICompatibleTranslator:
    """Translate JSON batches through OpenAI, Ollama, LM Studio, or compatible APIs."""

    def __init__(
        self,
        settings: TranslationSettings,
        secret_store: SecretStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | float = 120.0,
        backoff_base_seconds: float = 0.25,
    ) -> None:
        if not isinstance(settings, TranslationSettings):
            raise TypeError("settings must be TranslationSettings")
        if not settings.enabled:
            raise TranslationConfigurationError("翻譯未啟用；請改用 DisabledTranslator。")
        if backoff_base_seconds < 0:
            raise ValueError("backoff_base_seconds must not be negative")
        self.settings = settings
        self._secret_store = secret_store
        self._transport = transport
        self._timeout = timeout
        self._backoff_base_seconds = backoff_base_seconds
        self._endpoint = _chat_completions_url(settings.base_url)

    def translate(
        self,
        segments: Sequence[SubtitleSegment] | Sequence[Mapping[str, Any]],
        cancellation: CancellationToken | None = None,
    ) -> list[Translation]:
        """Translate all batches, retaining successes when another batch fails."""

        return run_async(self.atranslate(segments, cancellation))

    async def atranslate(
        self,
        segments: Sequence[SubtitleSegment] | Sequence[Mapping[str, Any]],
        cancellation: CancellationToken | None = None,
    ) -> list[Translation]:
        """Asynchronous implementation with per-batch bounded retries."""

        parsed = _parse_source_segments(segments)
        if not parsed:
            return []
        cancellation = cancellation or CancellationToken()
        cancellation.raise_if_cancelled()
        api_key = self._secret_store.load_provider_key(self.settings.provider_id)
        if not api_key and not _is_local_provider(self._endpoint):
            raise TranslationConfigurationError(
                "遠端翻譯服務必須先在 Windows Credential Manager 儲存 API 金鑰。"
            )
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        outcomes: dict[str, Translation] = {}
        async with httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
            follow_redirects=False,
        ) as client:
            for offset in range(0, len(parsed), self.settings.batch_size):
                cancellation.raise_if_cancelled()
                batch = parsed[offset : offset + self.settings.batch_size]
                try:
                    translated, attempts = await self._translate_batch_with_retry(
                        client,
                        headers,
                        batch,
                        cancellation,
                    )
                except RequestCancelled:
                    raise
                except TranslationError as exc:
                    safe_error = str(exc)
                    for item in batch:
                        outcomes[item.id] = Translation(
                            id=item.id,
                            text=None,
                            status=TranslationStatus.FAILED,
                            error=safe_error,
                            attempts=self.settings.max_retries + 1,
                        )
                else:
                    for item in batch:
                        outcomes[item.id] = Translation(
                            id=item.id,
                            text=translated[item.id],
                            status=TranslationStatus.COMPLETED,
                            attempts=attempts,
                        )

        return [outcomes[item.id] for item in parsed]

    async def _translate_batch_with_retry(
        self,
        client: httpx.AsyncClient,
        headers: Mapping[str, str],
        batch: Sequence[_SourceSegment],
        cancellation: CancellationToken,
    ) -> tuple[dict[str, str], int]:
        last_error: TranslationError | None = None
        for attempt in range(self.settings.max_retries + 1):
            cancellation.raise_if_cancelled()
            try:
                response = await request_with_cancellation(
                    client,
                    "POST",
                    self._endpoint,
                    headers=dict(headers),
                    json=self._request_payload(batch),
                    cancellation=cancellation,
                )
                translated = _parse_provider_response(response, batch)
                return translated, attempt + 1
            except RequestCancelled:
                raise
            except httpx.TimeoutException:
                last_error = TranslationProviderError(
                    "翻譯服務逾時。",
                    retryable=True,
                )
            except httpx.RequestError:
                last_error = TranslationProviderError(
                    "無法連線到翻譯服務。",
                    retryable=True,
                )
            except TranslationProviderError as exc:
                last_error = exc
            except TranslationProtocolError as exc:
                # A fresh completion can repair malformed/missing JSON.
                last_error = exc

            retryable = not isinstance(last_error, TranslationProviderError) or (
                last_error.retryable
            )
            if attempt >= self.settings.max_retries or not retryable:
                break
            delay = min(self._backoff_base_seconds * (2**attempt), 4.0)
            await cancellation.sleep(delay)

        if last_error is None:  # pragma: no cover - loop always executes
            raise TranslationError("翻譯批次失敗。")
        raise last_error

    def _request_payload(self, batch: Sequence[_SourceSegment]) -> dict[str, Any]:
        source_label = self.settings.source_language or "auto"
        system_parts = [
            "你是專業字幕翻譯器。",
            f"將 {source_label} 翻譯成 {self.settings.target_language}。",
            f"風格：{self.settings.style}。",
            '只輸出 JSON 物件，格式為 {"translations":[{"id":"原 id","text":"譯文"}]}。',
            "每個輸入 id 必須恰好出現一次，不可新增、遺漏或重新排序。",
            "保留換行數量，以及 HTML、ASS、printf 和雙大括號標記。",
        ]
        if self.settings.system_prompt.strip():
            system_parts.append(self.settings.system_prompt.strip())
        user_content = json.dumps(
            {"segments": [{"id": item.id, "text": item.text} for item in batch]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "temperature": self.settings.temperature,
            "messages": [
                {"role": "system", "content": "\n".join(system_parts)},
                {"role": "user", "content": user_content},
            ],
        }
        if self.settings.use_response_format:
            payload["response_format"] = {"type": "json_object"}
        return payload


@dataclass(frozen=True, slots=True)
class _SourceSegment:
    id: str
    text: str


def _parse_source_segments(
    segments: Sequence[SubtitleSegment] | Sequence[Mapping[str, Any]],
) -> list[_SourceSegment]:
    if isinstance(segments, (str, bytes)) or not isinstance(segments, Sequence):
        raise TypeError("segments must be a sequence")
    parsed: list[_SourceSegment] = []
    ids: set[str] = set()
    for index, segment in enumerate(segments):
        if isinstance(segment, Mapping):
            raw_id = segment.get("id")
            raw_text = (
                segment.get("source_text") if "source_text" in segment else segment.get("text")
            )
        else:
            raw_id = getattr(segment, "id", None)
            raw_text = getattr(segment, "source_text", None)
            if raw_text is None:
                raw_text = getattr(segment, "text", None)
        if not isinstance(raw_id, (str, int)) or isinstance(raw_id, bool):
            raise TypeError(f"segment {index} id must be a string or integer")
        segment_id = str(raw_id).strip()
        if not segment_id or segment_id in ids:
            raise ValueError(f"segment {index} id is empty or duplicated")
        if not isinstance(raw_text, str):
            raise TypeError(f"segment {index} text must be a string")
        ids.add(segment_id)
        parsed.append(_SourceSegment(segment_id, raw_text))
    return parsed


def _chat_completions_url(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise TranslationConfigurationError("翻譯 base URL 必須是有效的 http 或 https 網址。")
    if parsed.username is not None or parsed.password is not None:
        raise TranslationConfigurationError("翻譯 base URL 不得包含帳號或金鑰。")
    if parsed.query or parsed.fragment:
        raise TranslationConfigurationError("翻譯 base URL 不得包含 query 或 fragment。")
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path = f"{path}/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _is_local_provider(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")


def _parse_provider_response(
    response: httpx.Response,
    batch: Sequence[_SourceSegment],
) -> dict[str, str]:
    if response.status_code == 401:
        raise TranslationProviderError(
            "翻譯服務 API 金鑰無效。",
            status_code=401,
            retryable=False,
        )
    if response.status_code == 429:
        raise TranslationProviderError(
            "翻譯服務忙碌中或已達速率限制。",
            status_code=429,
            retryable=True,
        )
    if response.status_code >= 500:
        raise TranslationProviderError(
            f"翻譯服務暫時無法使用（HTTP {response.status_code}）。",
            status_code=response.status_code,
            retryable=True,
        )
    if response.status_code < 200 or response.status_code >= 300:
        raise TranslationProviderError(
            f"翻譯服務拒絕請求（HTTP {response.status_code}）。",
            status_code=response.status_code,
            retryable=False,
        )
    try:
        envelope = response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise TranslationProtocolError("翻譯服務回應不是有效 JSON。") from exc
    if not isinstance(envelope, dict):
        raise TranslationProtocolError("翻譯服務回應 envelope 必須是 JSON 物件。")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        raise TranslationProtocolError("翻譯服務回應缺少 choices。")
    first = choices[0]
    if not isinstance(first, dict) or not isinstance(first.get("message"), dict):
        raise TranslationProtocolError("翻譯服務回應缺少 message。")
    content = first["message"].get("content")
    if not isinstance(content, str):
        raise TranslationProtocolError("翻譯服務 message.content 必須是字串。")
    payload = _decode_json_content(content)
    if set(payload) != {"translations"}:
        raise TranslationProtocolError("翻譯 JSON 必須只包含 translations 欄位。")
    translations = payload["translations"]
    if not isinstance(translations, list):
        raise TranslationProtocolError("translations 必須是陣列。")

    expected = {item.id: item for item in batch}
    output: dict[str, str] = {}
    for index, item in enumerate(translations):
        if not isinstance(item, dict) or set(item) != {"id", "text"}:
            raise TranslationProtocolError(f"translations[{index}] 必須只包含 id 與 text。")
        raw_id = item["id"]
        text = item["text"]
        if not isinstance(raw_id, (str, int)) or isinstance(raw_id, bool):
            raise TranslationProtocolError(f"translations[{index}].id 格式無效。")
        segment_id = str(raw_id).strip()
        if segment_id in output:
            raise TranslationProtocolError(f"翻譯回應含重複 id：{segment_id}。")
        if segment_id not in expected:
            raise TranslationProtocolError(f"翻譯回應含未知 id：{segment_id}。")
        if not isinstance(text, str):
            raise TranslationProtocolError(f"translations[{index}].text 必須是字串。")
        if expected[segment_id].text.strip() and not text.strip():
            raise TranslationProtocolError(f"翻譯 id {segment_id} 的譯文不得為空白。")
        _validate_preserved_structure(expected[segment_id].text, text, segment_id)
        output[segment_id] = text

    missing = sorted(set(expected) - set(output))
    if missing:
        raise TranslationProtocolError(f"翻譯回應缺少 id：{', '.join(missing)}。")
    return output


def _decode_json_content(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) < 3 or lines[0].strip().lower() not in {"```", "```json"}:
            raise TranslationProtocolError("翻譯回應的 JSON code fence 格式無效。")
        if lines[-1].strip() != "```":
            raise TranslationProtocolError("翻譯回應的 JSON code fence 未正確結束。")
        stripped = "\n".join(lines[1:-1]).strip()
        if "```" in stripped:
            raise TranslationProtocolError("翻譯回應含多重 code fence。")
    elif "```" in stripped:
        raise TranslationProtocolError("翻譯回應含 JSON 以外的 code fence。")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise TranslationProtocolError("翻譯 message.content 不是有效 JSON。") from exc
    if not isinstance(payload, dict):
        raise TranslationProtocolError("翻譯 message.content 必須是 JSON 物件。")
    return payload


def _validate_preserved_structure(source: str, translated: str, segment_id: str) -> None:
    if source.count("\n") != translated.count("\n"):
        raise TranslationProtocolError(f"翻譯 id {segment_id} 未保留換行數量。")
    if Counter(_MARKER_PATTERN.findall(source)) != Counter(_MARKER_PATTERN.findall(translated)):
        raise TranslationProtocolError(f"翻譯 id {segment_id} 未完整保留格式標記。")
