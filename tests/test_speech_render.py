from __future__ import annotations

from hearflow.services.speech import _chunk_texts, _split_long_text


def test_chunking_never_exceeds_provider_limit() -> None:
    chunks = _chunk_texts(["第一句。" * 80, "Second sentence. " * 60], 240)
    assert len(chunks) > 2
    assert all(0 < len(item) <= 240 for item in chunks)


def test_long_unbroken_text_is_split_deterministically() -> None:
    pieces = _split_long_text("x" * 501, 200)
    assert [len(item) for item in pieces] == [200, 200, 101]
