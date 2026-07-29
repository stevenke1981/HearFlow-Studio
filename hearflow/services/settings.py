"""Typed, non-secret application settings and Windows credential storage."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

SETTINGS_KEY = "settings/app"
SETTINGS_SCHEMA_VERSION = 2
SECRET_SERVICE_NAME = "HearFlow Studio"


class SettingsError(RuntimeError):
    """Raised when settings cannot be loaded or persisted safely."""


class SecretStoreError(RuntimeError):
    """Raised when the operating-system secret store is unavailable."""


class _SettingsBackend(Protocol):
    def value(self, key: str, defaultValue: Any = None) -> Any: ...

    def setValue(self, key: str, value: Any) -> None: ...

    def sync(self) -> None: ...


class _KeyringBackend(Protocol):
    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


@dataclass(frozen=True, slots=True)
class EngineSettings:
    """Configuration that is safe to persist outside Credential Manager."""

    mode: str = "managed"
    backend: str = "cuda"
    model_id: str = "qwen3-asr-0.6b"
    model_repository: str = "ggml-org/Qwen3-ASR-0.6B-GGUF"
    model_quant: str = "Q8_0"
    gateway_url: str = "http://127.0.0.1:8000"
    credential_id: str = "qwen-gateway"
    runtime_dir: str = "runtime"
    gateway_port: int = 8000
    llama_port: int = 8080
    threads: int = 4
    context_size: int = 8192
    batch_size: int = 2048
    micro_batch_size: int = 512

    def __post_init__(self) -> None:
        if self.mode not in {"managed", "external"}:
            raise ValueError("engine mode must be 'managed' or 'external'")
        if self.backend not in {"cpu", "cuda", "vulkan"}:
            raise ValueError("engine backend must be cpu, cuda, or vulkan")
        _validate_http_url(self.gateway_url, "gateway_url")
        _validate_credential_id(self.credential_id)
        if self.mode == "managed" and not _is_loopback_url(self.gateway_url):
            raise ValueError("managed gateway_url must use 127.0.0.1 or localhost")
        if not self.model_id.strip():
            raise ValueError("model_id must not be empty")
        if not self.model_repository.strip() or not self.model_quant.strip():
            raise ValueError("model repository and quantization must not be empty")
        if not (1 <= self.gateway_port <= 65535 and 1 <= self.llama_port <= 65535):
            raise ValueError("engine ports must be between 1 and 65535")
        if self.gateway_port == self.llama_port:
            raise ValueError("gateway and llama-server must use different ports")
        for name in ("threads", "context_size", "batch_size", "micro_batch_size"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class RemoteTranscriptionSettings:
    """Remote speech-to-text configuration without credentials."""

    enabled: bool = False
    provider_id: str = "openai"
    credential_id: str = "openai"
    base_url: str = ""
    model: str = ""
    language: str = "auto"
    prompt: str = ""
    response_format: str = "json"
    temperature: float = 0.0
    max_file_bytes: int = 20 * 1024 * 1024
    http_referer: str = ""
    app_title: str = "HearFlow Studio"

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("remote transcription provider_id must not be empty")
        _validate_credential_id(self.credential_id)
        if self.base_url.strip():
            _validate_http_url(self.base_url, "remote transcription base_url")
        if self.enabled and not self.model.strip() and self.provider_id == "custom-openai":
            raise ValueError("custom remote transcription model must not be empty")
        if not self.language.strip():
            raise ValueError("remote transcription language must not be empty")
        if self.response_format not in {"json", "verbose_json", "diarized_json"}:
            raise ValueError("remote transcription response_format is unsupported")
        _validate_temperature(self.temperature, "remote transcription temperature", maximum=1)
        if not 1_048_576 <= self.max_file_bytes <= 2_147_483_648:
            raise ValueError("remote transcription max_file_bytes must be 1 MiB to 2 GiB")
        _validate_optional_http_url(self.http_referer, "remote transcription http_referer")


@dataclass(frozen=True, slots=True)
class TranslationSettings:
    """OpenAI-compatible translation settings without an API key."""

    enabled: bool = False
    provider_id: str = "openai"
    credential_id: str = "openai"
    base_url: str = ""
    model: str = ""
    source_language: str = "auto"
    target_language: str = "繁體中文（台灣）"
    style: str = "自然口語"
    system_prompt: str = ""
    batch_size: int = 20
    temperature: float = 0.2
    max_retries: int = 2
    use_response_format: bool = True
    http_referer: str = ""
    app_title: str = "HearFlow Studio"

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("translation provider_id must not be empty")
        _validate_credential_id(self.credential_id)
        if self.base_url.strip():
            _validate_http_url(self.base_url, "translation base_url")
        if self.enabled and not self.model.strip() and self.provider_id == "custom-openai":
            raise ValueError("custom translation model must not be empty")
        if not self.target_language.strip():
            raise ValueError("translation target_language must not be empty")
        if not 1 <= self.batch_size <= 200:
            raise ValueError("translation batch_size must be between 1 and 200")
        _validate_temperature(self.temperature, "translation temperature", maximum=2)
        if not 0 <= self.max_retries <= 10:
            raise ValueError("translation max_retries must be between 0 and 10")
        _validate_optional_http_url(self.http_referer, "translation http_referer")


@dataclass(frozen=True, slots=True)
class TranslationEngineSettings:
    """Managed llama.cpp translation engine settings."""

    enabled: bool = False
    backend: str = "cuda"
    model_id: str = "gemma-3-4b-it"
    model_repository: str = "ggml-org/gemma-3-4b-it-GGUF"
    model_quant: str = "Q4_K_M"
    port: int = 8081
    threads: int = 4
    context_size: int = 8192
    batch_size: int = 2048
    micro_batch_size: int = 512
    gpu_layers: int = 99

    def __post_init__(self) -> None:
        if self.backend not in {"cpu", "cuda", "vulkan"}:
            raise ValueError("translation engine backend must be cpu, cuda, or vulkan")
        if not self.model_id.strip():
            raise ValueError("translation engine model_id must not be empty")
        if not self.model_repository.strip() or not self.model_quant.strip():
            raise ValueError("translation engine model repository and quant must not be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("translation engine port must be between 1 and 65535")
        for name in ("threads", "context_size", "batch_size", "micro_batch_size"):
            if getattr(self, name) <= 0:
                raise ValueError(f"translation engine {name} must be positive")
        if self.gpu_layers < 0:
            raise ValueError("translation engine gpu_layers must not be negative")


@dataclass(frozen=True, slots=True)
class SpeechSettings:
    """Local Qwen3-TTS or remote text-to-speech settings without credentials."""

    enabled: bool = False
    mode: str = "local"
    provider_id: str = "openai"
    credential_id: str = "openai"
    base_url: str = ""
    model: str = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
    voice: str = "Vivian"
    language: str = "Chinese"
    output_format: str = "wav"
    speed: float = 1.0
    instructions: str = ""
    max_chars_per_request: int = 1600
    local_executable: str = ""
    local_model_dir: str = ""
    local_backend: str = "candle"
    local_talker_backend: str = "safetensors"
    local_talker_gguf: str = ""
    http_referer: str = ""
    app_title: str = "HearFlow Studio"

    def __post_init__(self) -> None:
        if self.mode not in {"local", "remote"}:
            raise ValueError("speech mode must be local or remote")
        if not self.provider_id.strip():
            raise ValueError("speech provider_id must not be empty")
        _validate_credential_id(self.credential_id)
        if self.base_url.strip():
            _validate_http_url(self.base_url, "speech base_url")
        if self.mode == "local" and not self.model.strip():
            raise ValueError("local speech model must not be empty")
        if self.mode == "remote" and self.provider_id == "custom-openai" and not self.model.strip():
            raise ValueError("custom remote speech model must not be empty")
        if not self.language.strip():
            raise ValueError("speech language must not be empty")
        if self.output_format not in {"wav", "mp3", "pcm", "aac", "flac", "opus"}:
            raise ValueError("speech output_format is unsupported")
        if isinstance(self.speed, bool) or not math.isfinite(float(self.speed)):
            raise ValueError("speech speed must be a finite number")
        if not 0.25 <= float(self.speed) <= 4.0:
            raise ValueError("speech speed must be between 0.25 and 4.0")
        if not 100 <= self.max_chars_per_request <= 10_000:
            raise ValueError("speech max_chars_per_request must be between 100 and 10000")
        if self.local_backend not in {"candle", "python"}:
            raise ValueError("speech local_backend must be candle or python")
        if self.local_talker_backend not in {"safetensors", "gguf"}:
            raise ValueError("speech local_talker_backend must be safetensors or gguf")
        _validate_optional_http_url(self.http_referer, "speech http_referer")


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Complete persistable application settings with no secret material."""

    schema_version: int = SETTINGS_SCHEMA_VERSION
    engine: EngineSettings = field(default_factory=EngineSettings)
    remote_transcription: RemoteTranscriptionSettings = field(
        default_factory=RemoteTranscriptionSettings
    )
    translation: TranslationSettings = field(default_factory=TranslationSettings)
    translation_engine: TranslationEngineSettings = field(
        default_factory=TranslationEngineSettings
    )
    speech: SpeechSettings = field(default_factory=SpeechSettings)
    recent_projects: tuple[str, ...] = ()
    last_project_dir: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != SETTINGS_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported settings schema {self.schema_version}; "
                f"expected {SETTINGS_SCHEMA_VERSION}"
            )
        if len(self.recent_projects) > 20:
            raise ValueError("recent_projects is limited to 20 entries")
        if len(set(self.recent_projects)) != len(self.recent_projects):
            raise ValueError("recent_projects must not contain duplicates")


class SecretStore:
    """Store provider credentials in the platform keyring."""

    def __init__(
        self,
        backend: _KeyringBackend | None = None,
        *,
        service_name: str = SECRET_SERVICE_NAME,
    ) -> None:
        if backend is None:
            try:
                import keyring
            except ImportError as exc:  # pragma: no cover - packaging guard
                raise SecretStoreError(
                    "找不到 keyring，無法使用 Windows Credential Manager。"
                ) from exc
            backend = keyring
        if not service_name.strip():
            raise ValueError("service_name must not be empty")
        self._backend = backend
        self._service_name = service_name

    def save(self, credential_id: str, secret: str) -> None:
        credential_id = _validate_credential_id(credential_id)
        if not isinstance(secret, str) or not secret:
            raise ValueError("secret must not be empty")
        try:
            self._backend.set_password(self._service_name, credential_id, secret)
        except Exception as exc:
            raise SecretStoreError("無法將金鑰寫入 Windows Credential Manager。") from exc

    def load(self, credential_id: str) -> str | None:
        credential_id = _validate_credential_id(credential_id)
        try:
            value = self._backend.get_password(self._service_name, credential_id)
        except Exception as exc:
            raise SecretStoreError("無法從 Windows Credential Manager 讀取金鑰。") from exc
        if value is not None and not isinstance(value, str):
            raise SecretStoreError("Credential Manager 回傳了無效的金鑰格式。")
        return value

    def delete(self, credential_id: str) -> bool:
        credential_id = _validate_credential_id(credential_id)
        try:
            if self._backend.get_password(self._service_name, credential_id) is None:
                return False
            self._backend.delete_password(self._service_name, credential_id)
        except Exception as exc:
            raise SecretStoreError("無法從 Windows Credential Manager 刪除金鑰。") from exc
        return True

    save_provider_key = save
    load_provider_key = load
    delete_provider_key = delete


class SettingsRepository:
    """Serialize typed settings through QSettings without persisting secrets."""

    def __init__(
        self,
        backend: _SettingsBackend | None = None,
        *,
        key: str = SETTINGS_KEY,
    ) -> None:
        if backend is None:
            try:
                from PySide6.QtCore import QSettings
            except ImportError as exc:  # pragma: no cover - packaging guard
                raise SettingsError("找不到 PySide6，無法初始化應用程式設定。") from exc
            backend = QSettings("HearFlow", "HearFlow Studio")
        self._backend = backend
        self._key = key

    def load(self) -> AppSettings:
        raw = self._backend.value(self._key, None)
        if raw in (None, ""):
            return AppSettings()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str):
            raise SettingsError("儲存的應用程式設定格式無效。")
        try:
            payload = json.loads(raw)
            return settings_from_dict(payload)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            raise SettingsError("無法讀取應用程式設定；設定內容已損毀或版本不相容。") from exc

    def save(self, settings: AppSettings) -> None:
        if not isinstance(settings, AppSettings):
            raise TypeError("settings must be an AppSettings instance")
        payload = settings_to_dict(settings)
        _reject_secret_fields(payload)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            self._backend.setValue(self._key, encoded)
            self._backend.sync()
            status = getattr(self._backend, "status", None)
            if callable(status):
                result = status()
                if int(result) != 0:
                    raise SettingsError("QSettings 回報寫入失敗。")
        except SettingsError:
            raise
        except Exception as exc:
            raise SettingsError("無法儲存應用程式設定。") from exc


def settings_to_dict(settings: AppSettings) -> dict[str, Any]:
    """Return a JSON-safe dictionary containing no secret material."""

    payload = asdict(settings)
    payload["recent_projects"] = list(settings.recent_projects)
    return payload


def settings_from_dict(payload: Mapping[str, Any]) -> AppSettings:
    """Validate, migrate, and construct :class:`AppSettings`."""

    if not isinstance(payload, Mapping):
        raise TypeError("settings payload must be an object")
    migrated = _migrate_settings(payload)
    allowed = {item.name for item in fields(AppSettings)}
    unknown = set(migrated) - allowed
    if unknown:
        raise ValueError(f"unknown settings fields: {', '.join(sorted(unknown))}")

    schema_version = _strict_int(migrated["schema_version"], "schema_version")
    engine = _dataclass_from_mapping(EngineSettings, migrated.get("engine", {}))
    remote_transcription = _dataclass_from_mapping(
        RemoteTranscriptionSettings,
        migrated.get("remote_transcription", {}),
    )
    translation = _dataclass_from_mapping(
        TranslationSettings,
        migrated.get("translation", {}),
    )
    translation_engine = _dataclass_from_mapping(
        TranslationEngineSettings,
        migrated.get("translation_engine", {}),
    )
    speech = _dataclass_from_mapping(SpeechSettings, migrated.get("speech", {}))

    recent_raw = migrated.get("recent_projects", ())
    if not isinstance(recent_raw, (list, tuple)) or not all(
        isinstance(item, str) for item in recent_raw
    ):
        raise TypeError("recent_projects must be a list of strings")
    last_project_dir = migrated.get("last_project_dir", "")
    if not isinstance(last_project_dir, str):
        raise TypeError("last_project_dir must be a string")

    return AppSettings(
        schema_version=schema_version,
        engine=engine,
        remote_transcription=remote_transcription,
        translation=translation,
        translation_engine=translation_engine,
        speech=speech,
        recent_projects=tuple(recent_raw),
        last_project_dir=last_project_dir,
    )


def _migrate_settings(payload: Mapping[str, Any]) -> dict[str, Any]:
    migrated = dict(payload)
    version = migrated.get("schema_version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise TypeError("schema_version must be an integer")
    if version == 1:
        translation_raw = migrated.get("translation", {})
        translation = dict(translation_raw) if isinstance(translation_raw, Mapping) else {}
        legacy_provider_id = str(translation.get("provider_id") or "default")
        # Version 1 used provider_id as both provider identity and credential key.
        # Its default value meant "generic OpenAI-compatible endpoint", not a
        # real provider, so migrate it to the explicit custom profile while
        # preserving the old Credential Manager lookup name.
        translation.setdefault("credential_id", legacy_provider_id)
        if legacy_provider_id == "default":
            translation["provider_id"] = "custom-openai"
        translation.setdefault("http_referer", "")
        translation.setdefault("app_title", "HearFlow Studio")
        migrated["translation"] = translation
        migrated.setdefault("remote_transcription", {})
        migrated.setdefault("speech", {})
        migrated["schema_version"] = 2
        version = 2
    if version != SETTINGS_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported settings schema {version}; expected {SETTINGS_SCHEMA_VERSION}"
        )
    return migrated


def _dataclass_from_mapping[T](cls: type[T], raw: Any) -> T:
    if not isinstance(raw, Mapping):
        raise TypeError(f"{cls.__name__} settings must be an object")
    allowed = {item.name for item in fields(cls)}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown {cls.__name__} fields: {', '.join(sorted(unknown))}")
    return cls(**dict(raw))


def _strict_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _validate_credential_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("credential_id must not be empty")
    if len(value) > 255 or "\0" in value or "\n" in value or "\r" in value:
        raise ValueError("credential_id is invalid")
    return value.strip()


def _validate_http_url(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{name} must be an http or https URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{name} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{name} must not contain a query or fragment")


def _validate_optional_http_url(value: str, name: str) -> None:
    if value.strip():
        _validate_http_url(value, name)


def _validate_temperature(value: float, name: str, *, maximum: float) -> None:
    if isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite")
    if not 0.0 <= float(value) <= maximum:
        raise ValueError(f"{name} must be between 0 and {maximum:g}")


def _is_loopback_url(value: str) -> bool:
    host = (urlsplit(value).hostname or "").lower()
    return host in {"127.0.0.1", "::1", "localhost"} or host.endswith(".localhost")


def _reject_secret_fields(payload: Mapping[str, Any], path: str = "") -> None:
    forbidden = {"api_key", "apikey", "secret", "password", "token"}
    for key, value in payload.items():
        current = f"{path}.{key}" if path else str(key)
        normalized = str(key).lower().replace("-", "_")
        if any(part in forbidden for part in normalized.split("_")):
            raise SettingsError(f"秘密欄位不得寫入設定：{current}")
        if isinstance(value, Mapping):
            _reject_secret_fields(value, current)


def resolve_runtime_dir(settings: EngineSettings, app_root: Path) -> Path:
    """Resolve a configured runtime directory without changing the process cwd."""

    configured = Path(settings.runtime_dir).expanduser()
    return configured if configured.is_absolute() else (app_root / configured).resolve()
