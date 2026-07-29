from __future__ import annotations

import pytest

from hearflow.services.provider_catalog import (
    ProviderCapability,
    get_provider_profile,
    normalized_base_url,
    profiles_for,
)


def test_provider_capabilities_do_not_invent_sakana_audio_support() -> None:
    sakana = get_provider_profile("sakana")
    assert sakana.supports(ProviderCapability.TRANSLATION)
    assert not sakana.supports(ProviderCapability.TRANSCRIPTION)
    assert not sakana.supports(ProviderCapability.SPEECH)


def test_required_provider_families_are_available() -> None:
    ids = {profile.id for profile in profiles_for(ProviderCapability.TRANSLATION)}
    assert {"openai", "xai", "openrouter", "gemini", "sakana", "local-llama"} <= ids


def test_gemini_uses_different_text_and_native_audio_bases() -> None:
    gemini = get_provider_profile("gemini")
    assert normalized_base_url(gemini, capability=ProviderCapability.TRANSLATION).endswith(
        "/v1beta/openai"
    )
    assert normalized_base_url(gemini, capability=ProviderCapability.TRANSCRIPTION).endswith(
        "/v1beta"
    )


def test_sakana_requires_customer_endpoint() -> None:
    with pytest.raises(ValueError, match="base URL"):
        normalized_base_url(
            get_provider_profile("sakana"), capability=ProviderCapability.TRANSLATION
        )
