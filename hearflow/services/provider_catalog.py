"""Provider capability catalog for local engines and remote AI APIs.

The catalog intentionally separates *capability* from *provider name*.  A text
provider is not automatically presented as an ASR or TTS provider, and each
operation can use a different wire protocol even when it shares one API key.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class ProviderCapability(StrEnum):
    """Features that a provider profile can expose to HearFlow."""

    TRANSCRIPTION = "transcription"
    TRANSLATION = "translation"
    SPEECH = "speech"


class AuthStyle(StrEnum):
    """Authentication styles used by supported HTTP APIs."""

    NONE = "none"
    BEARER = "bearer"
    GOOGLE_API_KEY = "google_api_key"


class SttApiStyle(StrEnum):
    """Request shapes used by speech-to-text endpoints."""

    NONE = "none"
    OPENAI_MULTIPART = "openai_multipart"
    OPENROUTER_JSON = "openrouter_json"
    XAI_MULTIPART = "xai_multipart"
    GEMINI_NATIVE = "gemini_native"


class TtsApiStyle(StrEnum):
    """Request shapes used by text-to-speech endpoints."""

    NONE = "none"
    OPENAI_SPEECH = "openai_speech"
    XAI_TTS = "xai_tts"
    GEMINI_NATIVE = "gemini_native"


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """One provider's stable endpoint and capability metadata.

    ``base_url`` never contains an API key.  Sakana Fugu uses an empty base URL
    because access endpoints are issued to customers; the UI therefore requires
    the user to paste the endpoint supplied by Sakana.
    """

    id: str
    label: str
    base_url: str
    capabilities: frozenset[ProviderCapability]
    translation_base_url: str = ""
    audio_base_url: str = ""
    translation_auth: AuthStyle = AuthStyle.BEARER
    audio_auth: AuthStyle = AuthStyle.BEARER
    stt_style: SttApiStyle = SttApiStyle.NONE
    tts_style: TtsApiStyle = TtsApiStyle.NONE
    default_translation_model: str = ""
    default_stt_model: str = ""
    default_tts_model: str = ""
    default_voice: str = ""
    default_language: str = "auto"
    supported_tts_formats: tuple[str, ...] = ()
    translation_response_format: bool = True
    notes: str = ""

    def supports(self, capability: ProviderCapability | str) -> bool:
        """Return whether this profile explicitly supports ``capability``."""

        try:
            parsed = ProviderCapability(capability)
        except ValueError:
            return False
        return parsed in self.capabilities


PROVIDER_PROFILES: Final[dict[str, ProviderProfile]] = {
    "local-llama": ProviderProfile(
        id="local-llama",
        label="本機 llama.cpp",
        base_url="http://127.0.0.1:8081/v1",
        capabilities=frozenset({ProviderCapability.TRANSLATION}),
        translation_auth=AuthStyle.BEARER,
        audio_auth=AuthStyle.NONE,
        default_translation_model="gemma-3-4b-it",
        notes="本機 CUDA/Vulkan/CPU 翻譯；API key 由 HearFlow 在行程內產生。",
    ),
    "openai": ProviderProfile(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        capabilities=frozenset(
            {
                ProviderCapability.TRANSCRIPTION,
                ProviderCapability.TRANSLATION,
                ProviderCapability.SPEECH,
            }
        ),
        stt_style=SttApiStyle.OPENAI_MULTIPART,
        tts_style=TtsApiStyle.OPENAI_SPEECH,
        default_translation_model="gpt-5-mini",
        default_stt_model="gpt-4o-mini-transcribe",
        default_tts_model="gpt-4o-mini-tts",
        default_voice="alloy",
        default_language="auto",
        supported_tts_formats=("mp3", "opus", "aac", "flac", "wav", "pcm"),
    ),
    "xai": ProviderProfile(
        id="xai",
        label="xAI",
        base_url="https://api.x.ai/v1",
        capabilities=frozenset(
            {
                ProviderCapability.TRANSCRIPTION,
                ProviderCapability.TRANSLATION,
                ProviderCapability.SPEECH,
            }
        ),
        stt_style=SttApiStyle.XAI_MULTIPART,
        tts_style=TtsApiStyle.XAI_TTS,
        default_translation_model="grok-4.5",
        default_stt_model="",
        default_tts_model="",
        default_voice="eve",
        default_language="auto",
        supported_tts_formats=("mp3", "wav", "pcm"),
        translation_response_format=True,
        notes="xAI 語音端點為 /v1/stt 與 /v1/tts，不是 OpenAI audio 路徑。",
    ),
    "openrouter": ProviderProfile(
        id="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        capabilities=frozenset(
            {
                ProviderCapability.TRANSCRIPTION,
                ProviderCapability.TRANSLATION,
                ProviderCapability.SPEECH,
            }
        ),
        stt_style=SttApiStyle.OPENROUTER_JSON,
        tts_style=TtsApiStyle.OPENAI_SPEECH,
        default_translation_model="openai/gpt-5-mini",
        default_stt_model="openai/gpt-4o-mini-transcribe",
        default_tts_model="openai/gpt-4o-mini-tts-2025-12-15",
        default_voice="alloy",
        default_language="auto",
        supported_tts_formats=("mp3", "pcm"),
        notes="STT 使用 base64 JSON；TTS 使用 OpenAI Audio Speech 相容端點。",
    ),
    "gemini": ProviderProfile(
        id="gemini",
        label="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        capabilities=frozenset(
            {
                ProviderCapability.TRANSCRIPTION,
                ProviderCapability.TRANSLATION,
                ProviderCapability.SPEECH,
            }
        ),
        translation_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        audio_base_url="https://generativelanguage.googleapis.com/v1beta",
        translation_auth=AuthStyle.BEARER,
        audio_auth=AuthStyle.GOOGLE_API_KEY,
        stt_style=SttApiStyle.GEMINI_NATIVE,
        tts_style=TtsApiStyle.GEMINI_NATIVE,
        default_translation_model="gemini-3.6-flash",
        default_stt_model="gemini-3.6-flash",
        default_tts_model="gemini-3.1-flash-tts-preview",
        default_voice="Kore",
        default_language="auto",
        supported_tts_formats=("wav", "pcm"),
        translation_response_format=True,
        notes="翻譯走 OpenAI compatibility；音訊理解與 TTS 走 Gemini native API。",
    ),
    "sakana": ProviderProfile(
        id="sakana",
        label="Sakana Fugu",
        base_url="",
        capabilities=frozenset({ProviderCapability.TRANSLATION}),
        translation_auth=AuthStyle.BEARER,
        audio_auth=AuthStyle.NONE,
        default_translation_model="fugu",
        translation_response_format=False,
        notes="Fugu 是 OpenAI 相容文字 API；請填入 Sakana 配發的 endpoint。",
    ),
    "custom-openai": ProviderProfile(
        id="custom-openai",
        label="自訂 OpenAI 相容服務",
        base_url="",
        capabilities=frozenset(
            {
                ProviderCapability.TRANSCRIPTION,
                ProviderCapability.TRANSLATION,
                ProviderCapability.SPEECH,
            }
        ),
        stt_style=SttApiStyle.OPENAI_MULTIPART,
        tts_style=TtsApiStyle.OPENAI_SPEECH,
        default_voice="alloy",
        default_language="auto",
        supported_tts_formats=("mp3", "opus", "aac", "flac", "wav", "pcm"),
        notes="能力取決於自訂服務；連線測試會逐項驗證。",
    ),
}


def get_provider_profile(provider_id: str) -> ProviderProfile:
    """Return a known provider profile, raising an actionable error otherwise."""

    normalized = provider_id.strip().casefold()
    try:
        return PROVIDER_PROFILES[normalized]
    except KeyError as exc:
        known = "、".join(profile.id for profile in PROVIDER_PROFILES.values())
        raise ValueError(f"未知供應商 {provider_id!r}；可用值：{known}") from exc


def profiles_for(capability: ProviderCapability | str) -> tuple[ProviderProfile, ...]:
    """Return provider profiles that explicitly advertise one capability."""

    parsed = ProviderCapability(capability)
    return tuple(profile for profile in PROVIDER_PROFILES.values() if profile.supports(parsed))


def normalized_base_url(
    profile: ProviderProfile,
    configured: str = "",
    *,
    capability: ProviderCapability | str | None = None,
) -> str:
    """Return a configured or capability-specific URL without a trailing slash."""

    parsed = ProviderCapability(capability) if capability is not None else None
    if parsed is ProviderCapability.TRANSLATION:
        default = profile.translation_base_url or profile.base_url
    elif parsed in {ProviderCapability.TRANSCRIPTION, ProviderCapability.SPEECH}:
        default = profile.audio_base_url or profile.base_url
    else:
        default = profile.base_url
    value = configured.strip() or default
    if not value:
        raise ValueError(f"{profile.label} 必須填入 API base URL。")
    return value.rstrip("/")
