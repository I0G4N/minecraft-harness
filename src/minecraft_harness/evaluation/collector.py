"""Privileged, operator-side evidence collection; never expose this class to agents.

An event's ``source='server'`` label is not authentication.  Only evidence registered
by this collector in the operator-owned registry is eligible for scoring.  The
registry and server world must be absent from player/agent container mounts.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
from typing import Any, Iterable
import uuid

from ..environment import read_level
from ..entity_evidence import end_crystal_summary

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SERVER_LINE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] \[Server thread/(?:INFO|WARN|ERROR)\]: (?P<message>.*)$")
LOG_CHUNK_BYTES = 1024 * 1024
LOG_COLLECTION_BYTES = 8 * LOG_CHUNK_BYTES


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def artifact_bytes(run_dir: Path, digest: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid artifact digest")
    path = run_dir / "artifacts" / digest
    if path.is_symlink():
        raise ValueError("Evidence artifact must not be a symlink")
    value = path.read_bytes()
    if hashlib.sha256(value).hexdigest() != digest:
        raise ValueError("Evidence artifact checksum mismatch")
    return value


class ServerEvidenceCollector:
    """Snapshot flushed server files into immutable, independently verifiable evidence.

    Call ``collect_world(..., baseline=True)`` before actions, then ``save-all
    flush`` (or stop the server) before later snapshots. Missing baseline files
    mean zero only if the trusted baseline snapshot records their absence.
    The caller owns the world epoch and must increment it after world rollback.
    """

    def __init__(self, run_dir: str | Path, recorder: Any):
        self.run_dir = Path(run_dir).resolve()
        self.recorder = recorder
        self._log_lock = threading.RLock()
        self.directory = self.run_dir / "evaluation"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.registry = self.directory / "server_evidence.sqlite3"
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS evidence ("
                "evidence_id TEXT PRIMARY KEY, kind TEXT NOT NULL, artifact_sha256 TEXT NOT NULL, "
                "world_epoch INTEGER NOT NULL, baseline INTEGER NOT NULL, event_seq INTEGER, "
                "metadata_json TEXT NOT NULL)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS evidence_seq ON evidence(event_seq)")
            connection.execute("CREATE TABLE IF NOT EXISTS log_cursors (path TEXT PRIMARY KEY, value_json TEXT NOT NULL)")
        os.chmod(self.registry, 0o600)
        self._reconcile_pending()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.registry, timeout=30)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def _reconcile_pending(self) -> None:
        """Link ledger commits interrupted before the second registry commit."""
        database = self.run_dir / "events.sqlite3"
        if not database.is_file():
            return
        with self._connect() as registry:
            pending = registry.execute("SELECT evidence_id,kind,artifact_sha256,world_epoch FROM evidence WHERE event_seq IS NULL").fetchall()
            if not pending:
                return
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as ledger:
                for identity, kind, digest, epoch in pending:
                    row = ledger.execute("SELECT event_seq FROM events WHERE kind=? AND actor_id='server_collector' AND source='server' AND world_epoch=? AND json_extract(payload_json,'$.evidence_id')=? AND json_extract(payload_json,'$.artifact_sha256')=? ORDER BY event_seq LIMIT 1", (kind, epoch, identity, digest)).fetchone()
                    if row:
                        registry.execute("UPDATE evidence SET event_seq=? WHERE evidence_id=?", (row[0], identity))

    def _artifact(self, value: bytes) -> str:
        if hasattr(self.recorder, "put_artifact"):
            # Share the recorder's quota reservation and atomic publication;
            # independent collector writes would undercount artifact storage.
            return self.recorder.put_artifact(value)["sha256"]
        digest = hashlib.sha256(value).hexdigest()
        directory = self.run_dir / "artifacts"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / digest
        if path.exists():
            artifact_bytes(self.run_dir, digest)
            return digest
        descriptor, temporary = tempfile.mkstemp(prefix=".evidence-", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                artifact_bytes(self.run_dir, digest)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            os.unlink(temporary)
        return digest

    def _record(self, kind: str, document: dict, world_epoch: int, baseline: bool,
                *, dedupe_key: str | None = None) -> dict:
        # Stable IDs reconcile a crash between the registry, ledger and cursor
        # commits. A replay must never manufacture a second log segment.
        evidence_id = str(uuid.uuid5(uuid.NAMESPACE_URL, dedupe_key)) if dedupe_key else str(uuid.uuid4())
        digest = self._artifact(canonical_json(document))
        metadata = {"schema_version": 1, "collector": "server-files-v1"}
        with self._connect() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO evidence VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (evidence_id, kind, digest, world_epoch, int(baseline), json.dumps(metadata)),
            ).rowcount == 1
            row = connection.execute("SELECT artifact_sha256,event_seq FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        digest, event_seq = row
        payload = {"evidence_id": evidence_id, "artifact_sha256": digest, "baseline": baseline}
        event = None
        if event_seq is not None:
            return {"event_seq": event_seq, "kind": kind, "actor_id": "server_collector",
                    "source": "server", "world_epoch": world_epoch, "payload": payload}
        # Recovery only: the ledger append may already have committed before a
        # power loss prevented registry linkage. Never trust an agent's copy.
        database = self.run_dir / "events.sqlite3"
        if not inserted and database.is_file():
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                found = connection.execute("SELECT event_seq FROM events WHERE kind=? AND actor_id='server_collector' AND source='server' AND json_extract(payload_json,'$.evidence_id')=? ORDER BY event_seq LIMIT 1", (kind, evidence_id)).fetchone()
                if found:
                    event = {"event_seq": found[0], "kind": kind, "actor_id": "server_collector",
                             "source": "server", "world_epoch": world_epoch, "payload": payload}
        if event is None:
            if hasattr(self.recorder, "append_server_event"):
                event = self.recorder.append_server_event(kind, payload, actor_id="server_collector", world_epoch=world_epoch)
            else:
                event = self.recorder.append(kind, payload, actor_id="server_collector", source="server", world_epoch=world_epoch)
        with self._connect() as connection:
            connection.execute("UPDATE evidence SET event_seq = ? WHERE evidence_id = ?", (event["event_seq"], evidence_id))
        return event

    def collect_world(
        self,
        world_dir: str | Path,
        *,
        world_epoch: int = 0,
        baseline: bool = False,
        player_ids: Iterable[str] | None = None,
    ) -> dict | None:
        """Collect stats/advancements; this method never commands or modifies a world."""
        world_dir = Path(world_dir).resolve(strict=True)
        if not world_dir.is_dir():
            raise ValueError("World path must be a directory")
        allowed = None if player_ids is None else {str(player).lower() for player in player_ids}
        if allowed is not None and any(not UUID_RE.fullmatch(player) for player in allowed):
            raise ValueError("player_ids must contain Minecraft UUIDs")
        files = {}
        for category in ("stats", "advancements"):
            directory = world_dir / category
            if directory.is_symlink():
                raise ValueError("Server evidence directory must not be a symlink")
            for path in sorted(directory.glob("*.json")):
                if not UUID_RE.fullmatch(path.stem) or (allowed is not None and path.stem.lower() not in allowed):
                    continue
                if path.is_symlink():
                    raise ValueError("Server evidence file must not be a symlink")
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError("Server evidence must be a regular file")
                raw = path.read_bytes()
                # Fail collection instead of certifying an incomplete/half-written save.
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise ValueError(f"Server evidence must be an object: {path.name}")
                files[f"{category}/{path.stem.lower()}.json"] = self._artifact(raw)
        level = None
        level_path = world_dir / "level.dat"
        if level_path.is_file() and not level_path.is_symlink():
            try:
                data = read_level(level_path)
                fields = {}
                for key in ("Time", "DayTime", "Day"):
                    value = data.get(key)
                    if type(value) is int and value >= 0:
                        fields[key.lower()] = value
                if fields:
                    fields["source"] = "world/level.dat"
                    level = fields
            except (OSError, ValueError, EOFError):
                # A half-written save must not invalidate other evidence; the
                # report marks in-game days unknown instead of guessing.
                level = {"error": "level_dat_unavailable"}
        document = {"schema_version": 1, "kind": "world_snapshot", "files": files,
                    "player_ids": sorted(allowed) if allowed is not None else None,
                    "scope": "explicit_players" if allowed is not None else "whole_experiment_world",
                    "end_crystals": end_crystal_summary(world_dir)}
        if level is not None:
            document["level"] = level
        if not baseline:
            digest = hashlib.sha256(canonical_json(document)).hexdigest()
            with self._connect() as connection:
                latest = connection.execute("SELECT artifact_sha256,baseline FROM evidence WHERE kind='server_world_snapshot' AND world_epoch=? AND event_seq IS NOT NULL ORDER BY event_seq DESC LIMIT 1", (world_epoch,)).fetchone()
            if latest and latest[0] == digest and not latest[1]:
                return None
        return self._record("server_world_snapshot", document, world_epoch, baseline)

    def collect_log(self, path: str | Path, *, world_epoch: int = 0,
                    source_timezone: str = "UTC", final: bool = False) -> dict | None:
        with self._log_lock:
            return self._collect_log(path,world_epoch=world_epoch,source_timezone=source_timezone,final=final)

    def log_status(self,path: str | Path) -> dict:
        """Latest trusted cursor observation, including unarchived tail bytes."""
        key=str(Path(path).absolute())
        with self._log_lock, self._connect() as connection:
            row=connection.execute('SELECT value_json FROM log_cursors WHERE path=?',(key,)).fetchone()
        return json.loads(row[0]) if row else {'pending_bytes':None,'observed_at':None}

    def _collect_log(self, path: str | Path, *, world_epoch: int = 0,
                    source_timezone: str = "UTC", final: bool = False) -> dict | None:
        """Incremental authoritative log evidence, draining renamed inode tails.

        If the previous inode is gone, record an explicit possible-tail gap;
        never pretend rotation implies that no unread bytes existed. Calls are
        byte-bounded and cursors commit after linked immutable evidence.
        """
        from .logs import normalize_segment
        path = Path(path)
        key = str(path.absolute())
        if path.is_symlink():
            raise ValueError("Server log must not be a symlink")
        with self._connect() as connection:
            row = connection.execute("SELECT value_json FROM log_cursors WHERE path=?", (key,)).fetchone()
        cursor = json.loads(row[0]) if row else None
        result = None
        consumed = 0

        def persist(value):
            with self._connect() as connection:
                connection.execute("INSERT OR REPLACE INTO log_cursors VALUES (?,?)", (key, json.dumps(value)))

        def open_regular(target):
            descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            stream = os.fdopen(descriptor, "rb")
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                stream.close()
                raise ValueError("Server log must be a regular file")
            return stream

        def drain(stream, value, epoch, final=False):
            nonlocal result, consumed
            metadata = os.fstat(stream.fileno())
            def observe():
                current=os.fstat(stream.fileno())
                value.update(observed_size=current.st_size,pending_bytes=max(0,current.st_size-value['offset']),
                             observed_at=datetime.now(timezone.utc).isoformat())
                persist(value)
                return value['pending_bytes']==0
            observe()
            while consumed < LOG_COLLECTION_BYTES:
                stream.seek(value["offset"])
                raw = stream.read(min(LOG_CHUNK_BYTES, LOG_COLLECTION_BYTES-consumed))
                if not raw: return observe()
                newline = raw.rfind(b"\n")
                if newline >= 0: raw = raw[:newline+1]
                elif len(raw) < LOG_CHUNK_BYTES:
                    # A byte-budget boundary is not an EOF. Preserve complete
                    # lines for the next call instead of inventing a fragment.
                    at_eof=stream.tell()>=os.fstat(stream.fileno()).st_size
                    if not final or not at_eof: observe();return at_eof
                # Keep the normalized event array bounded without dropping
                # facts in a chatty byte chunk. Remaining lines are read by the
                # next segment, rather than being reported as omitted metrics.
                boundary=-1
                for _ in range(4096):
                    boundary=raw.find(b'\n',boundary+1)
                    if boundary<0: break
                if boundary>=0: raw=raw[:boundary+1]
                normalized = normalize_segment(raw,cursor=value,path=key,generation=value['generation'],
                    offset=value['offset'],world_epoch=epoch,mtime=metadata.st_mtime,source_timezone=source_timezone)
                document = {"schema_version":3,"kind":"server_log_segment","path":key,
                    "device":value['device'],"inode":value['inode'],"generation":value['generation'],
                    "offset_start":value['offset'],"offset_end":value['offset']+len(raw),
                    "line_start":value['line'],"fragment_start":value['fragment'],
                    "raw_sha256":self._artifact(raw),**normalized}
                result = self._record("server_log_segment",document,epoch,False,
                    dedupe_key=f"log:{key}:{value['generation']}:{value['offset']}")
                committed=json.loads(artifact_bytes(self.run_dir,result['payload']['artifact_sha256']))
                committed_raw=artifact_bytes(self.run_dir,committed['raw_sha256'])
                value.update(offset=committed['offset_end'],line=committed['line_start']+committed_raw.count(b"\n"),
                    fragment=not committed_raw.endswith(b"\n"),world_epoch=epoch,
                    **committed.get('clock_after',{}))
                stream.seek(max(0,value['offset']-256))
                value['anchor']=hashlib.sha256(stream.read(min(256,value['offset']))).hexdigest()
                persist(value)
                consumed+=len(committed_raw)
            return observe()

        with open_regular(path) as stream:
            metadata=os.fstat(stream.fileno())
            same=bool(cursor and cursor['device']==metadata.st_dev and cursor['inode']==metadata.st_ino)
            intact=same and cursor['offset']<=metadata.st_size
            if intact and cursor['offset']:
                stream.seek(max(0,cursor['offset']-256))
                intact=hashlib.sha256(stream.read(min(256,cursor['offset']))).hexdigest()==cursor['anchor']
            if cursor and not intact:
                recovered=False
                tail_failure='rotated_inode_tail_unavailable'
                if not same:
                    for candidate in path.parent.iterdir():
                        if candidate.is_symlink(): continue
                        try: old=candidate.stat()
                        except FileNotFoundError: continue
                        if stat.S_ISREG(old.st_mode) and old.st_dev==cursor['device'] and old.st_ino==cursor['inode']:
                            with open_regular(candidate) as previous:
                                previous_metadata=os.fstat(previous.fileno())
                                previous.seek(max(0,cursor['offset']-256))
                                if (previous_metadata.st_size<cursor['offset'] or cursor['offset'] and hashlib.sha256(previous.read(min(256,cursor['offset']))).hexdigest()!=cursor['anchor']):
                                    tail_failure='rotated_inode_tail_truncated_or_rewritten';continue
                                if not drain(previous,cursor,cursor.get('world_epoch',world_epoch),final=True):
                                    return result
                            recovered=True
                            break
                if not recovered:
                    gap={'schema_version':1,'kind':'server_log_gap','path':key,'generation':cursor['generation'],
                         'offset_after_last_evidence':cursor['offset'],'received_at':datetime.now(timezone.utc).isoformat(),
                         'reason':tail_failure if not same else 'log_truncated_or_rewritten',
                         'lost_bytes':None,'completeness':'unknown_possible_unread_tail'}
                    result=self._record('server_log_gap',gap,cursor.get('world_epoch',world_epoch),False,
                        dedupe_key=f"log-gap:{key}:{cursor['generation']}:{cursor['offset']}")
                cursor=None
            if cursor is None:
                cursor={'device':metadata.st_dev,'inode':metadata.st_ino,'generation':uuid.uuid4().hex,
                        'offset':0,'line':1,'fragment':False,'anchor':'','world_epoch':world_epoch}
                persist(cursor)
            drain(stream,cursor,world_epoch,final=final)
        return result


def registered_evidence(run_dir: Path, *, after: int = 0) -> list[dict]:
    return list(iter_registered_evidence(run_dir, after=after))


def iter_registered_evidence(run_dir: Path, *, after: int = 0):
    """Open an existing registry read-only; never create it while reporting."""
    registry = run_dir / "evaluation" / "server_evidence.sqlite3"
    if not registry.is_file() or registry.is_symlink():
        return
    connection = sqlite3.connect(registry.as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        for row in connection.execute("SELECT * FROM evidence WHERE event_seq>? ORDER BY event_seq", (after,)):
            yield dict(row)
    finally:
        connection.close()
