from __future__ import annotations

from dataclasses import replace

from hearflow.services.service_factory import build_translator
from hearflow.services.settings import AppSettings, SecretStore
from hearflow.services.translation import OpenAICompatibleTranslator


class MemoryKeyring:
    def set_password(self, service_name: str, username: str, password: str) -> None:
        return None

    def get_password(self, service_name: str, username: str) -> str | None:
        return None

    def delete_password(self, service_name: str, username: str) -> None:
        return None


class LocalManager:
    base_url = "http://127.0.0.1:8081/v1"
    api_key = "generated-local-key"


def test_local_translation_uses_the_managed_llama_server_key() -> None:
    base = AppSettings()
    settings = replace(
        base,
        translation_engine=replace(base.translation_engine, enabled=True),
    )
    translator = build_translator(settings, SecretStore(MemoryKeyring()), LocalManager())
    assert isinstance(translator, OpenAICompatibleTranslator)
    assert translator._explicit_api_key == "generated-local-key"
    assert translator._endpoint == "http://127.0.0.1:8081/v1/chat/completions"
