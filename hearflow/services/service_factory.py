"""Centralized construction of local and remote AI services."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from hearflow.services.engine import EngineManager
from hearflow.services.gateway import QwenGatewayClient
from hearflow.services.local_tts import Qwen3TtsCliSynthesizer
from hearflow.services.provider_catalog import (
    ProviderCapability,
    get_provider_profile,
    normalized_base_url,
)
from hearflow.services.remote_audio import RemoteSpeechSynthesizer, RemoteTranscriptionClient
from hearflow.services.settings import AppSettings, SecretStore
from hearflow.services.speech import DisabledSpeechSynthesizer, SpeechSynthesizer
from hearflow.services.translation import DisabledTranslator, OpenAICompatibleTranslator, Translator
from hearflow.services.translation_engine import TranslationEngineManager


def build_transcription_client(
    settings: AppSettings,
    engine_manager: EngineManager,
    secret_store: SecretStore,
) -> QwenGatewayClient | RemoteTranscriptionClient:
    """Select local Qwen3-ASR/external Gateway or a remote STT provider."""

    if settings.remote_transcription.enabled:
        return RemoteTranscriptionClient(settings.remote_transcription, secret_store)
    if settings.engine.mode == "managed":
        return engine_manager.client
    gateway_key = (
        secret_store.load_provider_key(settings.engine.credential_id)
        or os.environ.get("HEARFLOW_GATEWAY_API_KEY")
    )
    return QwenGatewayClient(
        settings.engine.gateway_url,
        api_key=gateway_key,
        timeout=120.0,
    )


def build_translator(
    settings: AppSettings,
    secret_store: SecretStore,
    translation_engine_manager: TranslationEngineManager | None,
) -> Translator:
    """Build local llama.cpp translation or a remote OpenAI-compatible provider."""

    if settings.translation_engine.enabled:
        if translation_engine_manager is None:
            raise RuntimeError("本機翻譯已啟用，但 TranslationEngineManager 尚未建立。")
        local = replace(
            settings.translation,
            enabled=True,
            provider_id="local-llama",
            base_url=translation_engine_manager.base_url,
            model=settings.translation_engine.model_id,
            use_response_format=True,
        )
        return OpenAICompatibleTranslator(
            local,
            secret_store,
            api_key=translation_engine_manager.api_key,
        )
    if not settings.translation.enabled:
        return DisabledTranslator()

    profile = get_provider_profile(settings.translation.provider_id)
    if not profile.supports(ProviderCapability.TRANSLATION):
        raise ValueError(f"{profile.label} 沒有翻譯能力。")
    remote = replace(
        settings.translation,
        base_url=normalized_base_url(
            profile,
            settings.translation.base_url,
            capability=ProviderCapability.TRANSLATION,
        ),
        model=settings.translation.model or profile.default_translation_model,
        use_response_format=(
            settings.translation.use_response_format and profile.translation_response_format
        ),
    )
    extra_headers: dict[str, str] = {}
    if profile.id == "openrouter":
        if remote.http_referer.strip():
            extra_headers["HTTP-Referer"] = remote.http_referer.strip()
        if remote.app_title.strip():
            extra_headers["X-Title"] = remote.app_title.strip()
    return OpenAICompatibleTranslator(
        remote,
        secret_store,
        extra_headers=extra_headers,
    )


def build_speech_synthesizer(
    settings: AppSettings,
    secret_store: SecretStore,
    *,
    runtime_root: str | Path,
) -> SpeechSynthesizer:
    """Build local Qwen3-TTS CUDA or a remote provider."""

    if not settings.speech.enabled:
        return DisabledSpeechSynthesizer(settings.speech)
    if settings.speech.mode == "local":
        return Qwen3TtsCliSynthesizer(settings.speech, runtime_root=runtime_root)
    return RemoteSpeechSynthesizer(settings.speech, secret_store)
