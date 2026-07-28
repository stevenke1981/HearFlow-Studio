"""Shared filesystem utilities: atomic writes, hashing, safe names."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via a temporary file and ``os.replace``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return the SHA-256 hex digest of a file without loading it all into memory."""

    selected = Path(path).expanduser().resolve(strict=True)
    digest = hashlib.sha256()
    with selected.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_filename(value: str, *, max_length: int = 120) -> str:
    """Sanitise *value* into a Windows-safe filename stem."""

    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid else char for char in value).strip(" .")
    return (cleaned or "untitled")[:max_length]
