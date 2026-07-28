"""Real HTTP client for the qwen3-asr-llama-cpp Axum Gateway."""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import mimetypes
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from hearflow.domain.models import AsrResult, EngineStatus, SubtitleSegment

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=3600.0, write=300.0, pool=10.0)


class GatewayError(RuntimeError):
    """Actionable and secret-safe Gateway failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        code: str = "gateway_error",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.code = code


class GatewayProtocolError(GatewayError):
    """Raised when a successful response violates the documented API shape."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            retryable=False,
            code="invalid_gateway_response",
        )


class RequestCancelled(GatewayError):
    """Raised when HearFlow stops waiting for an HTTP operation."""

    def __init__(self) -> None:
        super().__init__(
            "已取消等待 ASR Gateway；外部 Gateway 的推論可能仍在執行。",
            retryable=False,
            code="cancelled",
        )


class CancellationToken:
    """Thread-safe cooperative cancellation shared by UI and HTTP workers."""

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise RequestCancelled()

    async def wait(self, poll_interval: float = 0.025) -> None:
        """Wait without pinning an executor thread that cannot be cancelled."""

        while not self.cancelled:
            await asyncio.sleep(poll_interval)

    async def sleep(self, seconds: float) -> None:
        """Cancellable delay used by bounded retry backoff."""

        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            self.raise_if_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.05, remaining))


@dataclass(frozen=True, slots=True)
class TranscriptionOptions:
    """Validated fields accepted by the upstream multipart endpoint."""

    model: str = "qwen3-asr"
    language: str = "auto"
    prompt: str = ""
    temperature: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must not be empty")
        if not isinstance(self.language, str) or not self.language.strip():
            raise ValueError("language must not be empty")
        if not isinstance(self.prompt, str):
            raise TypeError("prompt must be a string")
        if len(self.prompt.encode("utf-8")) > 4096:
            raise ValueError("prompt must not exceed 4096 UTF-8 bytes")
        if (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not math.isfinite(float(self.temperature))
            or not 0.0 <= float(self.temperature) <= 1.0
        ):
            raise ValueError("temperature must be between 0.0 and 1.0")


class QwenGatewayClient:
    """Call health and batch transcription endpoints without faking progress."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        api_key_provider: Callable[[], str | None] | None = None,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = _normalize_base_url(base_url)
        if api_key is not None and api_key_provider is not None:
            raise ValueError("provide api_key or api_key_provider, not both")
        self._api_key = api_key
        self._api_key_provider = api_key_provider
        self._timeout = timeout
        self._transport = transport

    def health(self, cancellation: CancellationToken | None = None) -> EngineStatus:
        """Return combined live/ready/model state instead of throwing offline."""

        return run_async(self.ahealth(cancellation))

    async def ahealth(self, cancellation: CancellationToken | None = None) -> EngineStatus:
        """Asynchronous health variant for non-blocking workers."""

        cancellation = cancellation or CancellationToken()
        cancellation.raise_if_cancelled()
        headers = self._authorization_headers()
        async with self._client() as client:
            live_task = asyncio.create_task(
                request_with_cancellation(
                    client,
                    "GET",
                    self._url("/health/live"),
                    cancellation=cancellation,
                )
            )
            ready_task = asyncio.create_task(
                request_with_cancellation(
                    client,
                    "GET",
                    self._url("/health/ready"),
                    cancellation=cancellation,
                )
            )
            models_task = asyncio.create_task(
                request_with_cancellation(
                    client,
                    "GET",
                    self._url("/v1/models"),
                    headers=headers,
                    cancellation=cancellation,
                )
            )
            responses = await asyncio.gather(
                live_task,
                ready_task,
                models_task,
                return_exceptions=True,
            )

        if cancellation.cancelled:
            raise RequestCancelled()

        live_response, ready_response, models_response = responses
        live = False
        ready = False
        models: tuple[str, ...] = ()
        details: list[str] = []
        status_code: int | None = None

        if isinstance(live_response, httpx.Response):
            status_code = live_response.status_code
            if live_response.status_code == 200:
                try:
                    payload = _json_object(live_response, "live")
                    live = payload.get("status") == "ok"
                    if not live:
                        details.append("Gateway live 回應狀態無效。")
                except GatewayProtocolError:
                    details.append("Gateway live 回應不是有效 JSON。")
            else:
                details.append(f"Gateway live 回傳 HTTP {live_response.status_code}。")
        else:
            details.append(_health_exception_detail(live_response, "Gateway 無法連線。"))

        if isinstance(ready_response, httpx.Response):
            if status_code is None or ready_response.status_code >= 400:
                status_code = ready_response.status_code
            if ready_response.status_code == 200:
                try:
                    payload = _json_object(ready_response, "ready")
                    ready = payload.get("status") == "ready"
                    if not ready:
                        details.append("llama-server 尚未就緒。")
                except GatewayProtocolError:
                    details.append("Gateway ready 回應不是有效 JSON。")
            elif ready_response.status_code == 503:
                details.append("llama-server 尚在載入或無法連線。")
            else:
                details.append(f"Gateway ready 回傳 HTTP {ready_response.status_code}。")
        else:
            details.append(_health_exception_detail(ready_response, "Gateway ready 檢查失敗。"))

        if isinstance(models_response, httpx.Response):
            if models_response.status_code == 200:
                try:
                    models = _parse_models(models_response)
                except GatewayProtocolError:
                    details.append("Gateway models 回應格式無效。")
            elif models_response.status_code == 401:
                status_code = 401
                details.append("Gateway API 金鑰無效。")
            else:
                if status_code is None:
                    status_code = models_response.status_code
                details.append(f"Gateway models 回傳 HTTP {models_response.status_code}。")
        else:
            details.append(_health_exception_detail(models_response, "Gateway models 檢查失敗。"))

        return EngineStatus(
            live=live,
            ready=ready,
            models=models,
            detail=" ".join(dict.fromkeys(details)),
            http_status=status_code,
        )

    def transcribe(
        self,
        media_path: str | Path,
        options: TranscriptionOptions | Mapping[str, Any] | None = None,
        cancellation: CancellationToken | None = None,
    ) -> AsrResult:
        """Upload one file and return a strictly validated verbose result."""

        return run_async(self.atranscribe(media_path, options, cancellation))

    async def atranscribe(
        self,
        media_path: str | Path,
        options: TranscriptionOptions | Mapping[str, Any] | None = None,
        cancellation: CancellationToken | None = None,
    ) -> AsrResult:
        """Asynchronous cancellable transcription implementation."""

        path = Path(media_path)
        if not path.exists():
            raise FileNotFoundError(path)
        if not path.is_file():
            raise ValueError("media_path must identify a file")
        if path.stat().st_size <= 0:
            raise ValueError("media file must not be empty")
        if options is None:
            parsed_options = TranscriptionOptions()
        elif isinstance(options, TranscriptionOptions):
            parsed_options = options
        elif isinstance(options, Mapping):
            parsed_options = TranscriptionOptions(**dict(options))
        else:
            raise TypeError("options must be TranscriptionOptions or a mapping")

        cancellation = cancellation or CancellationToken()
        cancellation.raise_if_cancelled()
        started = time.perf_counter()
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        form = {
            "model": parsed_options.model,
            "language": parsed_options.language,
            "prompt": parsed_options.prompt,
            "response_format": "verbose_json",
            "temperature": format(float(parsed_options.temperature), ".12g"),
        }

        try:
            with path.open("rb") as stream:
                async with self._client() as client:
                    response = await request_with_cancellation(
                        client,
                        "POST",
                        self._url("/v1/audio/transcriptions"),
                        headers=self._authorization_headers(),
                        files={"file": (path.name, stream, mime_type)},
                        data=form,
                        cancellation=cancellation,
                    )
        except RequestCancelled:
            raise
        except httpx.TimeoutException as exc:
            raise GatewayError(
                "等待 ASR Gateway 逾時；請確認模型已載入，或稍後重試。",
                retryable=True,
                code="timeout",
            ) from exc
        except httpx.RequestError as exc:
            raise GatewayError(
                "無法連線到 ASR Gateway；請檢查引擎狀態與網址。",
                retryable=True,
                code="connection_error",
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise _http_error(response.status_code)
        elapsed = time.perf_counter() - started
        payload = _json_object(response, "transcription")
        normalized = _validate_verbose_json(payload)
        headers = {str(key): str(value) for key, value in response.headers.items()}
        header_accuracy = response.headers.get("x-asr-timestamp-accuracy")
        if header_accuracy is not None and header_accuracy != "chunk":
            raise GatewayProtocolError("Gateway 回應標頭宣稱了非分片級時間戳，已拒絕寫入。")
        chunk_count = _parse_chunk_count(response.headers, len(normalized["segments"]))
        model = response.headers.get("x-asr-model") or parsed_options.model

        segments = [
            SubtitleSegment(
                id=str(item["id"]),
                start_ms=round(float(item["start"]) * 1000),
                end_ms=round(float(item["end"]) * 1000),
                source_text=str(item["text"]),
                status="raw",
                metadata={
                    "seek": int(item["seek"]),
                    "timestamp_accuracy": "chunk",
                },
            )
            for item in normalized["segments"]
        ]
        return AsrResult(
            raw_response=copy.deepcopy(payload),
            response_headers=headers,
            text=str(normalized["text"]),
            language=str(normalized["language"]),
            duration=float(normalized["duration"]),
            segments=segments,
            model=model,
            chunk_count=chunk_count,
            timestamp_accuracy="chunk",
            elapsed_seconds=elapsed,
        )

    def _authorization_headers(self) -> dict[str, str]:
        key = self._api_key_provider() if self._api_key_provider else self._api_key
        if key is None or key == "":
            return {}
        if not isinstance(key, str):
            raise TypeError("Gateway API key provider must return a string or None")
        return {"Authorization": f"Bearer {key}"}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
            follow_redirects=False,
        )

    def _url(self, route: str) -> str:
        return f"{self.base_url}{route}"


async def request_with_cancellation(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    cancellation: CancellationToken,
    **kwargs: Any,
) -> httpx.Response:
    """Cancel the in-flight httpx task when the UI cancellation token fires."""

    cancellation.raise_if_cancelled()
    request_task = asyncio.create_task(client.request(method, url, **kwargs))
    cancel_task = asyncio.create_task(cancellation.wait())
    try:
        done, _ = await asyncio.wait(
            {request_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation.cancelled:
            request_task.cancel()
            with suppress(asyncio.CancelledError):
                await request_task
            raise RequestCancelled()
        if request_task not in done:  # defensive; cancel task can only finish on cancel
            raise RequestCancelled()
        return await request_task
    finally:
        cancel_task.cancel()
        with suppress(asyncio.CancelledError):
            await cancel_task


def run_async[T](awaitable: Awaitable[T]) -> T:
    """Run an async service method from a Qt worker, even near an active loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    result: list[T] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(awaitable))
        except BaseException as exc:  # propagate the original service exception
            error.append(exc)

    thread = threading.Thread(target=runner, name="hearflow-http", daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]


def _normalize_base_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("base_url must not be empty")
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an http or https URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain a query or fragment")
    path = parsed.path.rstrip("/")
    if path == "/":
        path = ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _json_object(response: httpx.Response, endpoint: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise GatewayProtocolError(f"Gateway {endpoint} 回應不是有效 JSON。") from exc
    if not isinstance(payload, dict):
        raise GatewayProtocolError(f"Gateway {endpoint} JSON 必須是物件。")
    return payload


def _parse_models(response: httpx.Response) -> tuple[str, ...]:
    payload = _json_object(response, "models")
    data = payload.get("data")
    if not isinstance(data, list):
        raise GatewayProtocolError("Gateway models.data 必須是陣列。")
    values: list[str] = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise GatewayProtocolError("Gateway models 項目缺少字串 id。")
        model_id = item["id"].strip()
        if not model_id or model_id in values:
            raise GatewayProtocolError("Gateway models 包含空白或重複 id。")
        values.append(model_id)
    return tuple(values)


def _validate_verbose_json(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = {"task", "language", "duration", "text", "segments", "timestamp_accuracy"}
    missing = sorted(required - set(payload))
    if missing:
        raise GatewayProtocolError(f"Gateway verbose JSON 缺少必要欄位：{', '.join(missing)}。")
    if payload["task"] != "transcribe":
        raise GatewayProtocolError("Gateway verbose JSON task 必須是 transcribe。")
    if not isinstance(payload["language"], str) or not payload["language"].strip():
        raise GatewayProtocolError("Gateway verbose JSON language 必須是非空字串。")
    duration = _finite_number(payload["duration"], "duration")
    if duration < 0:
        raise GatewayProtocolError("Gateway verbose JSON duration 不得為負數。")
    if not isinstance(payload["text"], str):
        raise GatewayProtocolError("Gateway verbose JSON text 必須是字串。")
    if payload["timestamp_accuracy"] != "chunk":
        raise GatewayProtocolError(
            "Gateway 必須回報 timestamp_accuracy=chunk，不接受偽造的精細時間戳。"
        )
    raw_segments = payload["segments"]
    if not isinstance(raw_segments, list):
        raise GatewayProtocolError("Gateway verbose JSON segments 必須是陣列。")

    segment_ids: set[int] = set()
    previous_start = -1.0
    normalized_segments: list[dict[str, Any]] = []
    for index, item in enumerate(raw_segments):
        if not isinstance(item, dict):
            raise GatewayProtocolError(f"Gateway segment {index} 必須是物件。")
        missing_segment = {"id", "seek", "start", "end", "text"} - set(item)
        if missing_segment:
            raise GatewayProtocolError(
                f"Gateway segment {index} 缺少欄位：{', '.join(sorted(missing_segment))}。"
            )
        segment_id = item["id"]
        seek = item["seek"]
        if isinstance(segment_id, bool) or not isinstance(segment_id, int):
            raise GatewayProtocolError(f"Gateway segment {index} id 必須是整數。")
        if segment_id < 0 or segment_id in segment_ids:
            raise GatewayProtocolError(f"Gateway segment {index} id 無效或重複。")
        if isinstance(seek, bool) or not isinstance(seek, int) or seek < 0:
            raise GatewayProtocolError(f"Gateway segment {index} seek 必須是非負整數。")
        start = _finite_number(item["start"], f"segments[{index}].start")
        end = _finite_number(item["end"], f"segments[{index}].end")
        if start < 0 or end < start:
            raise GatewayProtocolError(f"Gateway segment {index} 時間範圍無效。")
        if start < previous_start:
            raise GatewayProtocolError("Gateway segments 必須依開始時間遞增。")
        if end > duration + 1.0:
            raise GatewayProtocolError(f"Gateway segment {index} 結束時間超過媒體時長。")
        if not isinstance(item["text"], str):
            raise GatewayProtocolError(f"Gateway segment {index} text 必須是字串。")
        segment_ids.add(segment_id)
        previous_start = start
        normalized_segments.append(
            {
                "id": segment_id,
                "seek": seek,
                "start": start,
                "end": end,
                "text": item["text"],
            }
        )

    return {
        "task": "transcribe",
        "language": payload["language"],
        "duration": duration,
        "text": payload["text"],
        "segments": normalized_segments,
        "timestamp_accuracy": "chunk",
    }


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GatewayProtocolError(f"Gateway verbose JSON {name} 必須是數字。")
    number = float(value)
    if not math.isfinite(number):
        raise GatewayProtocolError(f"Gateway verbose JSON {name} 必須是有限數字。")
    return number


def _parse_chunk_count(headers: httpx.Headers, default: int) -> int:
    raw = headers.get("x-asr-chunks")
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise GatewayProtocolError("X-ASR-Chunks 標頭不是整數。") from exc
    if value < 0:
        raise GatewayProtocolError("X-ASR-Chunks 標頭不得為負數。")
    if default and value != default:
        raise GatewayProtocolError("X-ASR-Chunks 與 verbose JSON segments 數量不一致。")
    return value


def _http_error(status_code: int) -> GatewayError:
    messages: dict[int, tuple[str, bool, str]] = {
        400: ("ASR 請求內容無效；請檢查模型、語言與媒體格式。", False, "bad_request"),
        401: ("ASR Gateway API 金鑰無效，請重新設定。", False, "unauthorized"),
        408: ("Gateway 處理或上傳逾時，請稍後重試或縮短媒體。", True, "timeout"),
        413: ("媒體超過 Gateway 允許的大小。", False, "payload_too_large"),
        429: ("ASR Gateway 忙碌中，請稍後重試。", True, "rate_limited"),
        502: ("llama-server 推論失敗或無回應，請重啟本機引擎。", True, "upstream"),
        503: ("ASR 引擎尚未就緒，請等待模型載入。", True, "not_ready"),
    }
    message, retryable, code = messages.get(
        status_code,
        (
            f"ASR Gateway 回傳 HTTP {status_code}。",
            status_code >= 500,
            "http_error",
        ),
    )
    return GatewayError(
        message,
        status_code=status_code,
        retryable=retryable,
        code=code,
    )


def _health_exception_detail(value: object, fallback: str) -> str:
    if isinstance(value, RequestCancelled):
        return "健康檢查已取消。"
    if isinstance(value, httpx.TimeoutException):
        return "Gateway 健康檢查逾時。"
    return fallback
