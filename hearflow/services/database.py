"""Canonical SQLite storage, migrations, and stale-result protection."""

from __future__ import annotations

import inspect
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from hearflow.domain.models import JobStatus, utc_now_iso

SCHEMA_VERSION = 4

_T = TypeVar("_T")
SqlParameters = Sequence[Any] | dict[str, Any]
Mutation = Callable[[sqlite3.Connection], Any] | Callable[[], Any]


class DatabaseVersionError(RuntimeError):
    """Raised when a project database is newer than this application."""


class ProjectDatabase:
    """Own the single canonical SQLite connection for a project.

    File-backed databases use WAL mode, foreign keys, a busy timeout, and explicit
    transactions.  The connection is guarded by an ``RLock`` and supports nested
    transactions through savepoints, which makes repository operations safe to
    compose.
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        """Open or create a project database and apply all known migrations."""

        if str(path) == ":memory:":
            self.path: Path | str = ":memory:"
        else:
            self.path = Path(path).expanduser().resolve(strict=False)
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._closed = False
        self._connection = sqlite3.connect(
            str(self.path),
            timeout=max(busy_timeout_ms, 1) / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._configure(busy_timeout_ms)
        self._migrate()

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the configured connection for transaction callbacks."""

        self._ensure_open()
        return self._connection

    @property
    def schema_version(self) -> int:
        """Return the SQLite ``user_version`` after migrations."""

        row = self.fetchone("PRAGMA user_version")
        return 0 if row is None else int(row[0])

    def _configure(self, busy_timeout_ms: int) -> None:
        """Apply connection-level durability and integrity settings."""

        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA busy_timeout = {max(busy_timeout_ms, 1)}")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        if self.path != ":memory:":
            mode = str(
                self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            ).casefold()
            if mode != "wal":
                raise sqlite3.OperationalError(f"SQLite 無法啟用 WAL 模式（目前為 {mode}）")

    def _migrate(self) -> None:
        """Atomically upgrade all older supported schema versions."""

        with self._lock:
            current = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise DatabaseVersionError(
                    f"專案資料庫版本 {current} 高於本程式支援的 {SCHEMA_VERSION}"
                )
            migrations = {
                1: _MIGRATION_1,
                2: _MIGRATION_2,
                3: _MIGRATION_3,
                4: _MIGRATION_4,
            }
            while current < SCHEMA_VERSION:
                target = current + 1
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in migrations[target]:
                        self._connection.execute(statement)
                    self._connection.execute(f"PRAGMA user_version = {target}")
                    self._connection.commit()
                except BaseException:
                    self._connection.rollback()
                    raise
                current = target

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Yield one atomic transaction and rollback on every exception.

        Nested calls use a savepoint.  A write transaction is ``IMMEDIATE`` by
        default so lock contention is discovered before partially executing a
        multi-table mutation.
        """

        self._ensure_open()
        with self._lock:
            depth = int(getattr(self._local, "depth", 0))
            savepoint = f"hearflow_sp_{depth}"
            if depth == 0:
                self._connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN DEFERRED")
            else:
                self._connection.execute(f"SAVEPOINT {savepoint}")
            self._local.depth = depth + 1
            try:
                yield self._connection
            except BaseException:
                if depth == 0:
                    self._connection.rollback()
                else:
                    self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                if depth == 0:
                    self._connection.commit()
                else:
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            finally:
                self._local.depth = depth

    def execute(self, sql: str, parameters: SqlParameters = ()) -> int:
        """Execute one modifying statement and return its affected-row count."""

        with self.transaction() as connection:
            cursor = connection.execute(sql, parameters)
            return cursor.rowcount

    def executemany(self, sql: str, parameter_rows: Sequence[SqlParameters]) -> int:
        """Execute a modifying statement for every parameter row."""

        with self.transaction() as connection:
            cursor = connection.executemany(sql, parameter_rows)
            return cursor.rowcount

    def fetchone(self, sql: str, parameters: SqlParameters = ()) -> sqlite3.Row | None:
        """Return the first query row, or ``None``."""

        self._ensure_open()
        with self._lock:
            return self._connection.execute(sql, parameters).fetchone()

    def fetchall(self, sql: str, parameters: SqlParameters = ()) -> list[sqlite3.Row]:
        """Return all query rows as ``sqlite3.Row`` instances."""

        self._ensure_open()
        with self._lock:
            return list(self._connection.execute(sql, parameters).fetchall())

    def begin_attempt(
        self,
        job_id: str,
        *,
        status: JobStatus | str = JobStatus.TRANSCRIBING,
    ) -> tuple[int, int]:
        """Start a new attempt and return ``(attempt_id, generation_id)``."""

        status_value = status.value if isinstance(status, JobStatus) else str(status)
        now = utc_now_iso()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT attempt_id, generation_id FROM media_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"找不到媒體工作：{job_id}")
            attempt_id = int(row["attempt_id"]) + 1
            generation_id = int(row["generation_id"])
            connection.execute(
                """
                UPDATE media_jobs
                   SET attempt_id = ?, status = ?, error_message = NULL, updated_at = ?
                 WHERE id = ?
                """,
                (attempt_id, status_value, now, job_id),
            )
            connection.execute(
                """
                INSERT INTO job_attempts(
                    job_id, attempt_id, generation_id, status, started_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, attempt_id, generation_id, status_value, now),
            )
        return attempt_id, generation_id

    def bump_generation(
        self,
        job_id: str,
        *,
        status: JobStatus | str = JobStatus.CANCELLED,
    ) -> int:
        """Invalidate all responses from the current generation and return the new id."""

        status_value = status.value if isinstance(status, JobStatus) else str(status)
        now = utc_now_iso()
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT project_id, attempt_id, generation_id
                  FROM media_jobs
                 WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"找不到媒體工作：{job_id}")
            old_generation = int(row["generation_id"])
            new_generation = old_generation + 1
            connection.execute(
                """
                UPDATE media_jobs
                   SET generation_id = ?, status = ?, updated_at = ?
                 WHERE id = ?
                """,
                (new_generation, status_value, now, job_id),
            )
            connection.execute(
                """
                UPDATE job_attempts
                   SET status = ?, finished_at = ?
                 WHERE job_id = ? AND attempt_id = ? AND generation_id = ?
                """,
                (
                    status_value,
                    now,
                    job_id,
                    int(row["attempt_id"]),
                    old_generation,
                ),
            )
            connection.execute(
                """
                INSERT INTO event_log(
                    project_id, job_id, level, event_type, message,
                    attempt_id, generation_id, created_at
                ) VALUES (?, ?, 'info', 'generation_bumped', ?, ?, ?, ?)
                """,
                (
                    str(row["project_id"]),
                    job_id,
                    f"generation {old_generation} 已失效",
                    int(row["attempt_id"]),
                    new_generation,
                    now,
                ),
            )
        return new_generation

    def integrity_check(self) -> bool:
        """Run SQLite's integrity check and return whether it reports ``ok``."""

        row = self.fetchone("PRAGMA integrity_check")
        return bool(row is not None and str(row[0]).casefold() == "ok")

    def close(self) -> None:
        """Checkpoint and close the database.  Calling twice is harmless."""

        with self._lock:
            if self._closed:
                return
            if self.path != ":memory:":
                self._connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
            self._connection.close()
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("ProjectDatabase 已關閉")

    def __enter__(self) -> ProjectDatabase:
        """Return this database for ``with`` usage."""

        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        """Close this database when leaving a ``with`` block."""

        self.close()


class AttemptGuard:
    """Compare-and-swap guard that suppresses late ASR/translation callbacks."""

    def __init__(self, database: ProjectDatabase, job_id: str | None = None) -> None:
        """Bind a database and optionally one job for repeated commits."""

        self._database = database
        self._job_id = job_id

    def commit_if_current(
        self,
        attempt_id: int,
        generation_id: int,
        mutation: Mutation,
        *,
        job_id: str | None = None,
    ) -> bool:
        """Run ``mutation`` only when the job still has the supplied CAS tokens.

        Rejected callbacks are recorded as a ``suppressed_stale_result`` event in
        the same transaction and never touch segments, artifacts, or job output.
        The callback may accept the active ``sqlite3.Connection`` or no arguments.
        """

        selected_job_id = job_id or self._job_id
        if not selected_job_id:
            raise ValueError("AttemptGuard 需要 job_id")
        now = utc_now_iso()
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT project_id, attempt_id, generation_id, status
                  FROM media_jobs
                 WHERE id = ?
                """,
                (selected_job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"找不到媒體工作：{selected_job_id}")
            current = (
                int(row["attempt_id"]) == int(attempt_id)
                and int(row["generation_id"]) == int(generation_id)
                and str(row["status"])
                not in {
                    JobStatus.CANCELLING.value,
                    JobStatus.CANCELLED.value,
                }
            )
            if not current:
                connection.execute(
                    """
                    INSERT INTO event_log(
                        project_id, job_id, level, event_type, message,
                        details_json, attempt_id, generation_id, created_at
                    ) VALUES (?, ?, 'warning', 'suppressed_stale_result', ?,
                              ?, ?, ?, ?)
                    """,
                    (
                        str(row["project_id"]),
                        selected_job_id,
                        "已拒絕晚到的舊工作結果",
                        json.dumps(
                            {
                                "received_attempt_id": int(attempt_id),
                                "received_generation_id": int(generation_id),
                                "current_attempt_id": int(row["attempt_id"]),
                                "current_generation_id": int(row["generation_id"]),
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        int(attempt_id),
                        int(generation_id),
                        now,
                    ),
                )
                return False
            _call_mutation(mutation, connection)
            return True


def _call_mutation(mutation: Mutation, connection: sqlite3.Connection) -> Any:
    """Call a CAS mutation with the connection when its signature accepts one."""

    try:
        signature = inspect.signature(mutation)
    except (TypeError, ValueError):
        return mutation(connection)  # type: ignore[call-arg]
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(
        parameter.kind is parameter.VAR_POSITIONAL for parameter in signature.parameters.values()
    )
    if positional or has_varargs:
        return mutation(connection)  # type: ignore[call-arg]
    return mutation()  # type: ignore[call-arg]


_MIGRATION_1 = (
    """
    CREATE TABLE projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        root_path TEXT NOT NULL UNIQUE,
        source_language TEXT NOT NULL DEFAULT 'auto',
        target_language TEXT,
        translation_style TEXT NOT NULL DEFAULT '台灣繁體中文',
        settings_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE media_jobs (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        source_path TEXT NOT NULL,
        source_name TEXT NOT NULL,
        fingerprint TEXT,
        size_bytes INTEGER NOT NULL DEFAULT 0 CHECK(size_bytes >= 0),
        status TEXT NOT NULL DEFAULT 'pending',
        translation_status TEXT NOT NULL DEFAULT 'not_requested',
        metadata_json TEXT,
        attempt_id INTEGER NOT NULL DEFAULT 0 CHECK(attempt_id >= 0),
        generation_id INTEGER NOT NULL DEFAULT 0 CHECK(generation_id >= 0),
        error_message TEXT,
        raw_response_json TEXT,
        response_headers_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(project_id, source_path)
    )
    """,
)

_MIGRATION_2 = (
    """
    CREATE TABLE subtitle_segments (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES media_jobs(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        start_ms INTEGER NOT NULL,
        end_ms INTEGER NOT NULL,
        source_text TEXT NOT NULL,
        translated_text TEXT,
        status TEXT NOT NULL DEFAULT 'raw',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        attempt_id INTEGER NOT NULL DEFAULT 0,
        generation_id INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(job_id, ordinal)
    )
    """,
    """
    CREATE TABLE artifacts (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES media_jobs(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        path TEXT NOT NULL,
        sha256 TEXT,
        size_bytes INTEGER NOT NULL DEFAULT 0 CHECK(size_bytes >= 0),
        attempt_id INTEGER NOT NULL DEFAULT 0,
        generation_id INTEGER NOT NULL DEFAULT 0,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(job_id, kind, path)
    )
    """,
    """
    CREATE TABLE event_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        job_id TEXT REFERENCES media_jobs(id) ON DELETE CASCADE,
        level TEXT NOT NULL,
        event_type TEXT NOT NULL,
        message TEXT NOT NULL,
        details_json TEXT NOT NULL DEFAULT '{}',
        attempt_id INTEGER,
        generation_id INTEGER,
        created_at TEXT NOT NULL
    )
    """,
)

_MIGRATION_3 = (
    """
    CREATE TABLE job_attempts (
        job_id TEXT NOT NULL REFERENCES media_jobs(id) ON DELETE CASCADE,
        attempt_id INTEGER NOT NULL CHECK(attempt_id >= 0),
        generation_id INTEGER NOT NULL CHECK(generation_id >= 0),
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        error_message TEXT,
        PRIMARY KEY(job_id, attempt_id, generation_id)
    )
    """,
    "CREATE INDEX idx_media_jobs_project_status ON media_jobs(project_id, status)",
    "CREATE INDEX idx_segments_job_ordinal ON subtitle_segments(job_id, ordinal)",
    "CREATE INDEX idx_artifacts_job_kind ON artifacts(job_id, kind)",
    "CREATE INDEX idx_events_project_created ON event_log(project_id, created_at)",
)

_MIGRATION_4 = (
    """
    CREATE TABLE subtitle_segments_v4 (
        id TEXT NOT NULL,
        job_id TEXT NOT NULL REFERENCES media_jobs(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        start_ms INTEGER NOT NULL,
        end_ms INTEGER NOT NULL,
        source_text TEXT NOT NULL,
        translated_text TEXT,
        status TEXT NOT NULL DEFAULT 'raw',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        attempt_id INTEGER NOT NULL DEFAULT 0,
        generation_id INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(job_id, id),
        UNIQUE(job_id, ordinal)
    )
    """,
    """
    INSERT INTO subtitle_segments_v4(
        id, job_id, ordinal, start_ms, end_ms, source_text, translated_text,
        status, metadata_json, attempt_id, generation_id, created_at, updated_at
    )
    SELECT id, job_id, ordinal, start_ms, end_ms, source_text, translated_text,
           status, metadata_json, attempt_id, generation_id, created_at, updated_at
      FROM subtitle_segments
    """,
    "DROP TABLE subtitle_segments",
    "ALTER TABLE subtitle_segments_v4 RENAME TO subtitle_segments",
    "CREATE INDEX idx_segments_job_ordinal ON subtitle_segments(job_id, ordinal)",
)
