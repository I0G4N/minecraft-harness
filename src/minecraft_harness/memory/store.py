from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


class MemoryConflict(ValueError):
    """The caller's revision is stale; read before writing again."""


class MemoryPermissionError(PermissionError):
    pass


class MemoryStore:
    """SQLite notes with atomic compare-and-swap and author-scoped permissions.

    This store belongs to the trusted harness. Its database must never be mounted
    in an agent shell container. ``actor_id`` is supplied by the harness, not by
    arguments emitted by a model.
    """

    def __init__(self, root: Path, recorder: Any = None, shared_enabled: bool = True,
                 max_author_bytes: int = 256_000_000, max_author_notes: int = 10000):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "memory.sqlite3"
        self.recorder = recorder
        self.shared_enabled = shared_enabled
        self.world_epoch = 0
        self.max_author_bytes, self.max_author_notes = max_author_bytes, max_author_notes
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS notes (
                scope TEXT NOT NULL, author TEXT NOT NULL, path TEXT NOT NULL,
                revision INTEGER NOT NULL, content TEXT NOT NULL,
                written_at TEXT NOT NULL, world_epoch INTEGER NOT NULL,
                evidence_refs TEXT NOT NULL,
                PRIMARY KEY(scope, author, path, revision))""")
            connection.execute("CREATE INDEX IF NOT EXISTS notes_author ON notes(author)")
            connection.execute("CREATE TABLE IF NOT EXISTS author_usage (author TEXT PRIMARY KEY, bytes INTEGER NOT NULL, notes INTEGER NOT NULL)")
            connection.execute("""INSERT OR IGNORE INTO author_usage
                SELECT author, SUM(length(CAST(content AS BLOB))+length(CAST(evidence_refs AS BLOB))+length(CAST(path AS BLOB))+512),
                COUNT(DISTINCT scope || ':' || path) FROM notes GROUP BY author""")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=1)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def close(self) -> None:
        # Operations already close their connections. Checkpoint the WAL only
        # after agent tasks have stopped, keeping the final run self-contained.
        with self._connect() as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    @staticmethod
    def _identity(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) or value in {".", ".."}:
            raise ValueError("Invalid agent identity")
        return value

    def _access(self, actor_id: str, scope: str, author: str | None, write: bool = False) -> str:
        self._identity(actor_id)
        owner = self._identity(author or actor_id)
        if scope not in {"private", "team"}:
            raise ValueError("scope must be private or team")
        if scope == "team" and not self.shared_enabled:
            raise MemoryPermissionError("Shared memory is disabled in this experiment")
        if owner != actor_id and (scope == "private" or write):
            raise MemoryPermissionError("Only an author can access private notes or write their notes")
        return owner

    @staticmethod
    def _note_path(value: str) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= 512:
            raise ValueError("Memory path must be text no longer than 512 characters")
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or "\x00" in value or "\\" in value or path == PurePosixPath("."):
            raise ValueError("Memory path must be a relative note path")
        return str(path)

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["evidence_refs"] = json.loads(result["evidence_refs"])
        result["provenance"] = "agent_claim"
        return result

    def read(self, actor_id: str, scope: str, path: str, author: str | None = None,
             revision: int | None = None, offset: int = 0, limit: int = 32000) -> dict[str, Any]:
        owner = self._access(actor_id, scope, author)
        query = "SELECT * FROM notes WHERE scope=? AND author=? AND path=?"
        params: list[Any] = [scope, owner, self._note_path(path)]
        if revision is not None:
            query += " AND revision=?"
            params.append(revision)
        query += " ORDER BY revision DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        if row is None:
            raise FileNotFoundError(f"No note: {scope}/{owner}/{path}")
        result = self._decode(row)
        offset, limit = max(0, int(offset)), max(1, min(int(limit), 32000))
        content = result["content"]
        result.update(content=content[offset:offset + limit], offset=offset,
                      next_offset=offset + limit if len(content) > offset + limit else None)
        return result

    def write(self, actor_id: str, scope: str, path: str, content: str,
              expected_revision: int, author: str | None = None,
              world_epoch: int | None = None, evidence_refs: list[Any] | None = None) -> dict[str, Any]:
        owner = self._access(actor_id, scope, author, write=True)
        path = self._note_path(path)
        if not isinstance(content, str) or len(content.encode()) > 1_000_000:
            raise ValueError("Note must be text no larger than 1 MB")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("expected_revision is required; use 0 for a new note")
        timestamp = datetime.now(timezone.utc).isoformat()
        epoch = self.world_epoch if world_epoch is None else world_epoch
        references = evidence_refs or []
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise ValueError("world_epoch must be a nonnegative integer")
        if not isinstance(references, list) or len(references) > 100:
            raise ValueError("At most 100 evidence references are allowed")
        encoded_references = json.dumps(references, allow_nan=False)
        if len(encoded_references.encode()) > 32000:
            raise ValueError("Evidence references exceed 32 KB")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT MAX(revision) FROM notes WHERE scope=? AND author=? AND path=?",
                (scope, owner, path)).fetchone()[0] or 0
            if current != expected_revision:
                raise MemoryConflict(f"Expected revision {expected_revision}, current revision is {current}")
            connection.execute("INSERT OR IGNORE INTO author_usage VALUES (?,0,0)", (owner,))
            size, notes = connection.execute("SELECT bytes,notes FROM author_usage WHERE author=?", (owner,)).fetchone()
            charge = len(content.encode()) + len(encoded_references.encode()) + len(path.encode()) + 512
            if size + charge > self.max_author_bytes:
                raise ValueError("Memory author storage quota exceeded")
            if not current and notes >= self.max_author_notes:
                raise ValueError("Memory author note quota exceeded")
            revision = current + 1
            connection.execute("INSERT INTO notes VALUES (?,?,?,?,?,?,?,?)",
                               (scope, owner, path, revision, content, timestamp, epoch, encoded_references))
            connection.execute("UPDATE author_usage SET bytes=bytes+?,notes=notes+? WHERE author=?", (charge, int(not current), owner))
        result = {"scope": scope, "author": owner, "path": path, "revision": revision,
                  "content": content, "written_at": timestamp, "world_epoch": epoch,
                  "evidence_refs": references, "provenance": "agent_claim"}
        if self.recorder is not None:
            self.recorder.append("memory_written", result, actor_id=actor_id, source="agent_claim", world_epoch=epoch)
        return result

    def list(self, actor_id: str, scope: str = "private", author: str | None = None,
             offset: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Memory list offset must be a nonnegative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Memory list limit must be between 1 and 1000")
        if scope == "team" and author is None:
            self._access(actor_id, scope, actor_id)
            owner_clause, params = "", [scope]
        else:
            owner = self._access(actor_id, scope, author)
            owner_clause, params = " AND author=?", [scope, owner]
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT scope,author,path,MAX(revision) AS revision FROM notes WHERE scope=?" + owner_clause +
                " GROUP BY scope,author,path ORDER BY author,path LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
        return [dict(row) for row in rows]
