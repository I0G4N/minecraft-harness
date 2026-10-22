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
            # Search projects only the latest revision of every note; the full
            # revision history stays in ``notes`` for CAS and explicit reads.
            connection.execute("""CREATE TABLE IF NOT EXISTS notes_latest (
                scope TEXT NOT NULL, author TEXT NOT NULL, path TEXT NOT NULL,
                revision INTEGER NOT NULL, content TEXT NOT NULL,
                written_at TEXT NOT NULL, world_epoch INTEGER NOT NULL,
                evidence_refs TEXT NOT NULL,
                PRIMARY KEY(scope, author, path))""")
            self.fts_enabled = False
            self._rebuild_latest(connection)
            self._init_search_index(connection)

    def _rebuild_latest(self, connection: sqlite3.Connection) -> None:
        """Repair the latest-revision projection from the durable notes table.

        Older databases predate the projection; this is also the restart-safe
        recovery path when no search side table exists yet.
        """
        connection.execute("""DELETE FROM notes_latest WHERE NOT EXISTS (
            SELECT 1 FROM notes AS n
            WHERE n.scope=notes_latest.scope AND n.author=notes_latest.author
              AND n.path=notes_latest.path AND n.revision=notes_latest.revision)""")
        connection.execute("""INSERT INTO notes_latest
            (scope, author, path, revision, content, written_at, world_epoch, evidence_refs)
            SELECT n.scope, n.author, n.path, n.revision, n.content,
                   n.written_at, n.world_epoch, n.evidence_refs
            FROM notes AS n
            JOIN (SELECT scope, author, path, MAX(revision) AS revision
                  FROM notes GROUP BY scope, author, path) AS newest
              ON n.scope=newest.scope AND n.author=newest.author
             AND n.path=newest.path AND n.revision=newest.revision
            WHERE NOT EXISTS (SELECT 1 FROM notes_latest AS latest
                              WHERE latest.scope=n.scope
                                AND latest.author=n.author
                                AND latest.path=n.path)""")

    def _init_search_index(self, connection: sqlite3.Connection) -> None:
        """Create the FTS5 index (or degrade to the LIKE fallback) and rebuild.

        The index stores only the latest revision of each note. If FTS5 is not
        available in this SQLite build, ``search()`` transparently falls back to
        a bounded LIKE scan over the same projection.
        """
        try:
            connection.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                scope UNINDEXED, author UNINDEXED, path UNINDEXED,
                revision UNINDEXED, written_at UNINDEXED, world_epoch UNINDEXED,
                content)""")
        except sqlite3.OperationalError:
            self.fts_enabled = False
            return
        self.fts_enabled = True
        indexed = connection.execute("SELECT COUNT(*) FROM notes_fts").fetchone()[0]
        current = connection.execute("SELECT COUNT(*) FROM notes_latest").fetchone()[0]
        stale = connection.execute("""SELECT COUNT(*) FROM notes_fts AS f
            WHERE NOT EXISTS (SELECT 1 FROM notes_latest AS l
                WHERE l.rowid=f.rowid AND l.scope=f.scope AND l.author=f.author
                  AND l.path=f.path AND l.revision=f.revision)""").fetchone()[0]
        if indexed != current or stale:
            connection.execute("DELETE FROM notes_fts")
            connection.execute("""INSERT INTO notes_fts
                (rowid, scope, author, path, revision, written_at, world_epoch, content)
                SELECT rowid, scope, author, path, revision, written_at, world_epoch, content
                FROM notes_latest""")

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
            # Keep the latest-revision projection and its search index in step.
            replaced = connection.execute(
                "SELECT rowid FROM notes_latest WHERE scope=? AND author=? AND path=?",
                (scope, owner, path)).fetchone()
            if replaced is not None and self.fts_enabled:
                connection.execute("DELETE FROM notes_fts WHERE rowid=?", (replaced[0],))
            connection.execute("DELETE FROM notes_latest WHERE scope=? AND author=? AND path=?",
                               (scope, owner, path))
            inserted = connection.execute("""INSERT INTO notes_latest
                (scope, author, path, revision, content, written_at, world_epoch, evidence_refs)
                VALUES (?,?,?,?,?,?,?,?)""",
                (scope, owner, path, revision, content, timestamp, epoch, encoded_references))
            if self.fts_enabled:
                connection.execute("""INSERT INTO notes_fts
                    (rowid, scope, author, path, revision, written_at, world_epoch, content)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (inserted.lastrowid, scope, owner, path, revision, timestamp, epoch, content))
        result = {"scope": scope, "author": owner, "path": path, "revision": revision,
                  "content": content, "written_at": timestamp, "world_epoch": epoch,
                  "evidence_refs": references, "provenance": "agent_claim"}
        if self.recorder is not None:
            self.recorder.append("memory_written", result, actor_id=actor_id, source="agent_claim", world_epoch=epoch)
        return result

    @staticmethod
    def _search_terms(query: str) -> list[str]:
        """Split a user query into bounded literal FTS terms (never raw syntax)."""
        terms: list[str] = []
        for term in re.findall(r"\w+", query.lower(), re.UNICODE):
            if term not in terms:
                terms.append(term)
            if len(terms) >= 16:
                break
        return terms

    @staticmethod
    def _like_snippet(content: str, terms: list[str], width: int = 80) -> str:
        """Bounded snippet with the earliest matching query term highlighted."""
        lowered = content.lower()
        positions = [lowered.find(term) for term in terms]
        positions = [position for position in positions if position >= 0]
        if not positions:
            return content[:width] + ("..." if len(content) > width else "")
        position = min(positions)
        start = max(0, position - width // 3)
        end = min(len(content), start + width)
        body = content[start:end]
        for term in sorted(terms, key=len, reverse=True):
            match = re.search(re.escape(term), body, re.IGNORECASE)
            if match:
                body = body[:match.start()] + "[" + body[match.start():match.end()] + "]" + body[match.end():]
                break
        return ("..." if start > 0 else "") + body + ("..." if end < len(content) else "")

    def _search_scope(self, actor_id: str, scope: str,
                      author: str | None) -> tuple[str, list[Any]] | None:
        """Resolve the caller's accessible scope to a SQL predicate + parameters.

        Private notes are always author-only; team notes are readable across
        authors only when shared memory is enabled. ``None`` means the request
        cannot see anything (e.g. team notes while sharing is disabled).
        """
        self._identity(actor_id)
        if scope not in {"all", "private", "team"}:
            raise ValueError("Memory search scope must be all, private or team")
        if author is not None:
            author = self._identity(author)
        conditions: list[str] = []
        params: list[Any] = []
        if scope == "private":
            owner = self._access(actor_id, "private", author)
            conditions.append("scope='private' AND author=?")
            params.append(owner)
        elif scope == "team":
            if not self.shared_enabled:
                raise MemoryPermissionError("Shared memory is disabled in this experiment")
            if author is None:
                conditions.append("scope='team'")
            else:
                conditions.append("scope='team' AND author=?")
                params.append(author)
        elif author is None:
            conditions.append("scope='private' AND author=?")
            params.append(actor_id)
            if self.shared_enabled:
                conditions.append("scope='team'")
        elif author == actor_id:
            conditions.append("scope='private' AND author=?")
            params.append(actor_id)
            if self.shared_enabled:
                conditions.append("scope='team' AND author=?")
                params.append(author)
        elif self.shared_enabled:
            conditions.append("scope='team' AND author=?")
            params.append(author)
        else:
            return None
        if not conditions:
            return None
        return "(" + " OR ".join("(" + condition + ")" for condition in conditions) + ")", params

    def _search_fts(self, terms: list[str], where: str, params: list[Any],
                    limit: int) -> list[dict[str, Any]]:
        # Quote every term so FTS5 operators in user text cannot inject syntax.
        match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        query = ("SELECT scope, author, path, revision, written_at, world_epoch, "
                 "snippet(notes_fts, 6, '[', ']', '...', 24) AS snippet, "
                 "bm25(notes_fts) AS relevance FROM notes_fts "
                 "WHERE notes_fts MATCH ? AND " + where +
                 " ORDER BY relevance ASC, author, path LIMIT ?")
        with self._connect() as connection:
            rows = connection.execute(query, [match, *params, limit]).fetchall()
        return [{"scope": row["scope"], "author": row["author"], "path": row["path"],
                 "revision": row["revision"], "written_at": row["written_at"],
                 "world_epoch": row["world_epoch"], "snippet": row["snippet"],
                 "rank": -float(row["relevance"]), "mode": "fts5"} for row in rows]

    def _search_like(self, terms: list[str], where: str, params: list[Any],
                     limit: int) -> list[dict[str, Any]]:
        escaped = [term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                   for term in terms]
        clauses = " OR ".join("(content LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\')"
                              for _ in escaped)
        query = ("SELECT scope, author, path, revision, written_at, world_epoch, content "
                 "FROM notes_latest WHERE " + where + " AND (" + clauses + ") LIMIT 500")
        arguments: list[Any] = [*params]
        for term in escaped:
            arguments.extend((f"%{term}%", f"%{term}%"))
        with self._connect() as connection:
            rows = connection.execute(query, arguments).fetchall()
        hits = []
        for row in rows:
            content = row["content"]
            lowered = content.lower()
            score = sum(lowered.count(term) for term in terms)
            rank = score / (1.0 + len(content) / 1024.0)
            hits.append({"scope": row["scope"], "author": row["author"],
                         "path": row["path"], "revision": row["revision"],
                         "written_at": row["written_at"],
                         "world_epoch": row["world_epoch"],
                         "snippet": self._like_snippet(content, terms),
                         "rank": rank, "mode": "like"})
        hits.sort(key=lambda item: (-item["rank"], item["author"], item["path"]))
        return hits[:limit]

    def search(self, actor_id: str, query: str, scope: str = "all",
               author: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Ranked full-text search over the latest revision of accessible notes.

        Returns bounded snippets plus note metadata only; the caller's ``actor_id``
        decides private/team visibility, so the harness (not the model) supplies it.
        FTS5 is used when available, with a bounded LIKE scan as fallback.
        """
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 1000:
            raise ValueError("Memory search query must be 1-1000 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Memory search limit must be between 1 and 100")
        terms = self._search_terms(query)
        if not terms:
            return []
        plan = self._search_scope(actor_id, scope, author)
        if plan is None:
            return []
        where, params = plan
        if self.fts_enabled:
            try:
                return self._search_fts(terms, where, params, limit)
            except sqlite3.DatabaseError:
                # A malformed index/build must not break the agent: use the
                # documented bounded fallback for this call.
                pass
        return self._search_like(terms, where, params, limit)

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
