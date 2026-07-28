"""Human-readable and machine-readable project acceptance reports."""

from __future__ import annotations

import json
import platform
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from hearflow.domain.models import MediaJob, QaIssue, SubtitleSegment, utc_now_iso
from hearflow.services.fsutil import atomic_write_text, safe_filename


class ReportService:
    """Generate UTF-8 Markdown and JSON reports from canonical data."""

    def generate_job_report(
        self,
        *,
        project_name: str,
        job: MediaJob,
        segments: list[SubtitleSegment],
        issues: list[QaIssue],
        output_dir: str | Path,
        engine: dict[str, Any] | None = None,
    ) -> tuple[Path, Path]:
        destination = Path(output_dir).expanduser().resolve(strict=False)
        destination.mkdir(parents=True, exist_ok=True)
        stem = safe_filename(job.source_path.stem)
        json_path = _versioned(destination / f"{stem}.report.json")
        markdown_path = json_path.with_suffix(".md")
        severity_counts = Counter(issue.severity.value for issue in issues)
        translated = sum(item.translated_text is not None for item in segments)
        payload: dict[str, Any] = {
            "schema": "hearflow.report.v1",
            "generated_at": utc_now_iso(),
            "project": project_name,
            "job": job.to_dict(),
            "summary": {
                "segments": len(segments),
                "translated_segments": translated,
                "source_duration_ms": sum(max(0, item.duration_ms) for item in segments),
                "qa": dict(severity_counts),
                "timestamp_accuracy": "chunk"
                if any(item.metadata.get("timestamp_accuracy") == "chunk" for item in segments)
                else "edited_or_imported",
            },
            "issues": [issue.to_dict() for issue in issues],
            "engine": engine or {},
            "runtime": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
            },
        }
        atomic_write_text(
            json_path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        rows = "\n".join(
            f"| {issue.severity.value} | {issue.code} | "
            f"{issue.segment_id or '—'} | {_escape_cell(issue.message)} |"
            for issue in issues
        )
        if not rows:
            rows = "| — | — | — | 未發現問題 |"
        markdown = f"""# HearFlow 工作報告

- 專案：{project_name}
- 檔案：{job.source_path.name}
- 狀態：{job.status.value}
- 字幕片段：{len(segments)}
- 已翻譯：{translated}
- 產生時間：{payload["generated_at"]}

> 時間精度：{payload["summary"]["timestamp_accuracy"]}。上游 Qwen3-ASR
> 目前提供分片級近似時間，並非逐字對齊或說話者分離。

## QA 摘要

- blocking：{severity_counts.get("blocking", 0)}
- error：{severity_counts.get("error", 0)}
- warning：{severity_counts.get("warning", 0)}
- info：{severity_counts.get("info", 0)}

## QA 明細

| 等級 | 代碼 | 片段 | 說明 |
|---|---|---|---|
{rows}
"""
        atomic_write_text(markdown_path, markdown)
        return markdown_path, json_path


def _versioned(path: Path) -> Path:
    if not path.exists() and not path.with_suffix(".md").exists():
        return path
    for version in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}.v{version}{path.suffix}")
        if not candidate.exists() and not candidate.with_suffix(".md").exists():
            return candidate
    raise RuntimeError("找不到可用的報告檔名。")


def _escape_cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\n", "<br>")
