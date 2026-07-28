from __future__ import annotations

import json

import httpx

from hearflow.domain.models import SubtitleSegment, TranslationStatus
from hearflow.services.settings import TranslationSettings
from hearflow.services.translation import DisabledTranslator, OpenAICompatibleTranslator


class _Secrets:
    def __init__(self, key: str | None = "test-key") -> None:
        self.key = key

    def load_provider_key(self, provider_id: str) -> str | None:
        assert provider_id == "test"
        return self.key


def _settings() -> TranslationSettings:
    return TranslationSettings(
        enabled=True,
        provider_id="test",
        base_url="https://translator.test/v1",
        model="local-text-model",
        source_language="English",
        target_language="繁體中文（台灣）",
        batch_size=20,
        max_retries=0,
    )


def test_disabled_translation_is_explicit_skip_with_null_target() -> None:
    source = [SubtitleSegment("a", 0, 1_000, "Do not copy me")]
    result = DisabledTranslator().translate(source)

    assert result[0].status is TranslationStatus.SKIPPED
    assert result[0].text is None
    assert result[0].attempts == 0


def test_openai_compatible_translation_maps_by_id_and_preserves_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        user = json.loads(payload["messages"][1]["content"])
        assert [item["id"] for item in user["segments"]] == ["a", "b"]
        content = json.dumps(
            {
                "translations": [
                    {"id": "b", "text": "第二行\n保留 {{name}}"},
                    {"id": "a", "text": "第一行"},
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    translator = OpenAICompatibleTranslator(
        _settings(),
        _Secrets(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    source = [
        SubtitleSegment("a", 0, 1_000, "First"),
        SubtitleSegment("b", 1_000, 2_000, "Second\nKeep {{name}}"),
    ]
    outcomes = translator.translate(source)

    assert [item.id for item in outcomes] == ["a", "b"]
    assert [item.text for item in outcomes] == [
        "第一行",
        "第二行\n保留 {{name}}",
    ]
    assert all(item.status is TranslationStatus.COMPLETED for item in outcomes)


def test_strict_mapping_fails_closed_when_provider_omits_id() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        content = json.dumps(
            {"translations": [{"id": "a", "text": "只有一段"}]},
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    translator = OpenAICompatibleTranslator(
        _settings(),
        _Secrets(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    outcomes = translator.translate(
        [
            SubtitleSegment("a", 0, 1_000, "One"),
            SubtitleSegment("b", 1_000, 2_000, "Two"),
        ]
    )

    assert [item.status for item in outcomes] == [
        TranslationStatus.FAILED,
        TranslationStatus.FAILED,
    ]
    assert all(item.text is None for item in outcomes)
    assert all(item.error and "缺少 id" in item.error for item in outcomes)


def test_translation_rejects_blank_target_for_nonblank_source() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        content = json.dumps(
            {"translations": [{"id": "a", "text": "   "}]},
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    translator = OpenAICompatibleTranslator(
        _settings(),
        _Secrets(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    outcomes = translator.translate([SubtitleSegment("a", 0, 1_000, "Source")])

    assert outcomes[0].status is TranslationStatus.FAILED
    assert outcomes[0].text is None
    assert outcomes[0].error and "不得為空白" in outcomes[0].error
