from __future__ import annotations

import json
from dataclasses import replace

from hearflow.services.settings import (
    AppSettings,
    SecretStore,
    SettingsRepository,
    settings_from_dict,
)


class MemorySettings:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def value(self, key: str, defaultValue: object = None) -> object:
        return self.values.get(key, defaultValue)

    def setValue(self, key: str, value: object) -> None:
        self.values[key] = value

    def sync(self) -> None:
        return None


class MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


def test_v1_settings_migrate_without_changing_legacy_credential_name() -> None:
    settings = settings_from_dict(
        {
            "schema_version": 1,
            "engine": {},
            "translation": {
                "enabled": True,
                "provider_id": "default",
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "qwen3:8b",
            },
            "translation_engine": {},
            "recent_projects": [],
            "last_project_dir": "",
        }
    )
    assert settings.schema_version == 2
    assert settings.translation.provider_id == "custom-openai"
    assert settings.translation.credential_id == "default"
    assert settings.remote_transcription.enabled is False
    assert settings.speech.enabled is False


def test_settings_repository_never_persists_api_key() -> None:
    backend = MemorySettings()
    repository = SettingsRepository(backend)
    settings = replace(
        AppSettings(),
        recent_projects=("C:/Projects/HearFlow",),
    )
    repository.save(settings)
    encoded = backend.values["settings/app"]
    assert isinstance(encoded, str)
    payload = json.loads(encoded)
    assert payload["schema_version"] == 2
    assert "api_key" not in encoded.casefold()
    assert "password" not in encoded.casefold()
    assert repository.load() == settings


def test_secret_store_uses_credential_id_not_provider_name() -> None:
    keyring = MemoryKeyring()
    store = SecretStore(keyring)
    store.save_provider_key("openai-production", "secret-value")
    assert store.load_provider_key("openai-production") == "secret-value"
    assert store.load_provider_key("openai") is None
