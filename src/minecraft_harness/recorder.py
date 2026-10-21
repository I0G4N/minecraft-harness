"""Durable event ledger; commits transport data before allowing acknowledgement."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path

class StorageLimitError(RuntimeError):
    pass

class TransportGapError(RuntimeError):
    pass

def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

class Recorder:
    def __init__(self, run_dir, run_id=None, limits=None):
        self.run_dir = Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        manifest = self.run_dir / 'manifest.json'
        saved_id = json.loads(manifest.read_text()).get('run_id') if manifest.exists() else None
        self.run_id = run_id or saved_id or self.run_dir.name
        self.limits = limits or {}
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.closed = False
        self.artifact_dir = self.run_dir / 'artifacts'
        self.artifact_dir.mkdir(exist_ok=True)
        self.artifact_bytes = sum(self._file_sizes(self.artifact_dir))
        self.db = sqlite3.connect(self.run_dir / 'events.sqlite3', check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA busy_timeout=10000')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS events (
                event_seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                actor_id TEXT NOT NULL, source TEXT NOT NULL, received_at TEXT NOT NULL,
                world_epoch INTEGER NOT NULL, runtime_epoch INTEGER NOT NULL,
                execution_id TEXT, model_call_id TEXT, payload_json TEXT NOT NULL,
                monotonic_elapsed_ms INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS events_kind_actor_seq ON events(kind,actor_id,event_seq);
            CREATE INDEX IF NOT EXISTS events_kind_seq ON events(kind,event_seq);
            CREATE INDEX IF NOT EXISTS events_execution_actor_seq ON events(execution_id,actor_id,event_seq);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS submissions (agent_id TEXT NOT NULL, request_id TEXT NOT NULL,
                digest TEXT NOT NULL, value_json TEXT NOT NULL, PRIMARY KEY(agent_id, request_id));
            CREATE INDEX IF NOT EXISTS submissions_session_status ON submissions(agent_id,json_extract(value_json,'$.session_id'),json_extract(value_json,'$.status'));
            CREATE INDEX IF NOT EXISTS submissions_execution ON submissions(json_extract(value_json,'$.execution_id'));
            CREATE TABLE IF NOT EXISTS transport_cursors (agent_id TEXT NOT NULL, session_id TEXT NOT NULL,
                cursor INTEGER NOT NULL, PRIMARY KEY(agent_id,session_id));
            CREATE TABLE IF NOT EXISTS executions (agent_id TEXT NOT NULL, execution_id TEXT NOT NULL,
                value_json TEXT NOT NULL, PRIMARY KEY(agent_id,execution_id));
            CREATE TABLE IF NOT EXISTS controls (control_id TEXT PRIMARY KEY, command TEXT NOT NULL,
                arguments_json TEXT NOT NULL, state TEXT NOT NULL, requested_at TEXT NOT NULL, result_json TEXT);
        ''')
        self.db.commit()

    @staticmethod
    def _json(value):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)

    def _insert(self, kind, payload, actor_id='supervisor', source='harness', execution_id=None,
                world_epoch=0, runtime_epoch=0, model_call_id=None):
        encoded = self._json(payload)
        if len(encoded.encode()) > self.limits.get('max_event_bytes', 4*1024**2):
            raise StorageLimitError('event exceeds bounded ledger input; offload content to an artifact')
        cursor = self.db.execute('INSERT INTO events(kind,actor_id,source,received_at,world_epoch,runtime_epoch,execution_id,model_call_id,payload_json,monotonic_elapsed_ms) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (kind, actor_id, source, datetime.now(timezone.utc).isoformat(), world_epoch, runtime_epoch,
             execution_id, model_call_id, encoded, int((time.monotonic()-self.started)*1000)))
        return self._event(self.db.execute('SELECT * FROM events WHERE event_seq=?', (cursor.lastrowid,)).fetchone())

    def append(self, kind, payload, **kwargs):
        with self.lock, self.db:
            return self._insert(kind, payload, **kwargs)

    def append_server_event(self, kind, payload, **kwargs):
        kwargs['source'] = 'server'
        return self.append(kind, payload, **kwargs)

    def _event(self, row):
        value = dict(row)
        value['payload'] = json.loads(value.pop('payload_json'))
        value.update(schema_version=1, run_id=self.run_id)
        return value

    def read_events(self, after=0, limit=1000):
        if type(after) is not int or after < 0 or type(limit) is not int or limit < 1:
            raise ValueError('positive page size and nonnegative cursor required')
        with self.lock:
            result, size = [], 0
            for row in self.db.execute('SELECT * FROM events WHERE event_seq>? ORDER BY event_seq LIMIT ?', (after,min(limit,10000))):
                encoded_size = len(row['payload_json'].encode())
                if result and size + encoded_size > 8*1024**2:
                    break
                result.append(self._event(row))
                size += encoded_size
            return result

    def latest_event_seq(self):
        with self.lock:
            return self.db.execute('SELECT COALESCE(MAX(event_seq),0) FROM events').fetchone()[0]

    def all_events(self):
        # A separate read snapshot streams one bounded row, rather than loading
        # 1000 potentially 4 MiB payloads into memory during final export.
        path = self.run_dir / 'events.sqlite3'
        connection = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            for row in connection.execute('SELECT * FROM events ORDER BY event_seq'):
                yield self._event(row)
        finally:
            connection.close()

    def player_events(self, agent_id, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or limit < 1:
            raise ValueError('positive page size and nonnegative cursor required')
        with self.lock:
            result, size = [], 0
            for row in self.db.execute("SELECT * FROM events WHERE kind='player_event' AND actor_id=? AND event_seq>? ORDER BY event_seq LIMIT ?", (agent_id,after,min(limit,1000))):
                encoded_size = len(row['payload_json'].encode())
                if result and size + encoded_size > 8*1024**2:
                    break
                result.append({**json.loads(row['payload_json']),'cursor':row['event_seq']})
                size += encoded_size
            return result

    @staticmethod
    def _file_digest(path):
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            while chunk := stream.read(1024*1024):
                digest.update(chunk)
        return digest.hexdigest()

    def put_artifact(self, data):
        if isinstance(data, str):
            data = data.encode('utf-8')
        digest = hashlib.sha256(data).hexdigest()
        target = self.artifact_dir / digest
        with self.lock:
            if target.exists():
                if target.is_symlink() or self._file_digest(target)!=digest:
                    raise ValueError('artifact integrity violation')
                return {'hash':digest,'sha256':digest,'path':str(target.relative_to(self.run_dir))}
            if self.artifact_bytes+len(data)>self.limits.get('max_artifact_bytes',16*1024**3):
                raise StorageLimitError('artifact quota exhausted')
            if shutil.disk_usage(self.run_dir).free-len(data)<self.limits.get('min_free_disk_bytes',256*1024**2):
                raise StorageLimitError('disk reserve would be exhausted')
            descriptor, temporary = tempfile.mkstemp(prefix='.pending-',dir=self.artifact_dir)
            try:
                with os.fdopen(descriptor,'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary,target)
                fsync_directory(self.artifact_dir)
                self.artifact_bytes += len(data)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return {'hash':digest,'sha256':digest,'path':str(target.relative_to(self.run_dir))}

    def get_state(self,key,default=None):
        with self.lock:
            row=self.db.execute('SELECT value_json FROM state WHERE key=?',(key,)).fetchone()
            return json.loads(row[0]) if row else default

    def _set_state(self,key,value):
        self.db.execute('INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json',(key,self._json(value)))

    def set_state(self,key,value):
        with self.lock,self.db:
            self._set_state(key,value)

    def state_event(self,key,value,kind,payload,**kwargs):
        with self.lock,self.db:
            self._set_state(key,value)
            return self._insert(kind,payload,**kwargs)

    def submission(self,agent_id,request_id,digest=None,value=None):
        with self.lock,self.db:
            row=self.db.execute('SELECT * FROM submissions WHERE agent_id=? AND request_id=?',(agent_id,request_id)).fetchone()
            if row:
                if digest is not None and row['digest']!=digest:
                    raise ValueError('request_id reused with different program or timeout')
                return json.loads(row['value_json'])
            if value is not None:
                self.db.execute('INSERT INTO submissions VALUES (?,?,?,?)',(agent_id,request_id,digest,self._json(value)))
                return value
            return None

    def reserve_submission(self,agent_id,request_id,digest,value):
        with self.lock,self.db:
            row=self.db.execute('SELECT * FROM submissions WHERE agent_id=? AND request_id=?',(agent_id,request_id)).fetchone()
            if row:
                if row['digest']!=digest:
                    raise ValueError('request_id reused with different program or timeout')
                return json.loads(row['value_json']),False
            key='program_sequence:'+agent_id
            row=self.db.execute('SELECT value_json FROM state WHERE key=?',(key,)).fetchone()
            sequence=(json.loads(row[0]) if row else 0)+1
            self._set_state(key,sequence)
            record={**value,'request_sequence':sequence}
            self.db.execute('INSERT INTO submissions VALUES (?,?,?,?)',(agent_id,request_id,digest,self._json(record)))
            self._insert('execution_submitted',record,actor_id=agent_id,world_epoch=record.get('world_epoch',0))
            return record,True

    TERMINAL = {'succeeded', 'failed', 'timed_out', 'cancelled', 'interrupted', 'rejected', 'invalidated'}

    def _update_submission(self, agent_id, request_id, value):
        row = self.db.execute('SELECT value_json FROM submissions WHERE agent_id=? AND request_id=?', (agent_id, request_id)).fetchone()
        if row:
            old = json.loads(row[0])
            if old.get('status') in self.TERMINAL:
                value = old
            else:
                value = {**old, **value}
            self.db.execute('UPDATE submissions SET value_json=? WHERE agent_id=? AND request_id=?', (self._json(value), agent_id, request_id))
        return value

    def update_submission(self,agent_id,request_id,value):
        with self.lock,self.db:
            value = self._update_submission(agent_id,request_id,value)
            if value.get('execution_id'):
                self._save_execution(agent_id,value)

    def _save_execution(self,agent_id,record):
        row = self.db.execute('SELECT value_json FROM executions WHERE agent_id=? AND execution_id=?', (agent_id,record['execution_id'])).fetchone()
        if row and json.loads(row[0]).get('status') in self.TERMINAL:
            return
        self.db.execute('INSERT INTO executions VALUES (?,?,?) ON CONFLICT(agent_id,execution_id) DO UPDATE SET value_json=excluded.value_json',(agent_id,record['execution_id'],self._json(record)))
        if record.get('request_id'):
            self._update_submission(agent_id,record['request_id'],record)

    def settle_session(self, agent_id, session_id, reason='old worker process exit confirmed'):
        """End unresolved handles only after fencing; interrupted does not imply action rollback."""
        with self.lock,self.db:
            rows = self.db.execute("SELECT request_id,value_json FROM submissions WHERE agent_id=? AND json_extract(value_json,'$.session_id')=? AND json_extract(value_json,'$.status') NOT IN ('succeeded','failed','timed_out','cancelled','interrupted','rejected','invalidated')", (agent_id,session_id)).fetchall()
            for row in rows:
                record = {**json.loads(row['value_json']), 'status':'interrupted', 'proven_old_worker_stopped':True,
                          'result_unknown':True, 'error':reason+'; action effects may have occurred; never replay'}
                self._update_submission(agent_id,row['request_id'],record)
                if record.get('execution_id'):
                    self._save_execution(agent_id,record)
                self._insert('execution_interrupted',record,actor_id=agent_id,execution_id=record.get('execution_id'),world_epoch=record.get('world_epoch',0))
            return len(rows)

    def execution(self,agent_id,execution_id):
        with self.lock:
            row=self.db.execute('SELECT value_json FROM executions WHERE agent_id=? AND execution_id=?',(agent_id,execution_id)).fetchone()
            return json.loads(row[0]) if row else None

    def transport_cursor(self,agent_id,session_id):
        with self.lock:
            row=self.db.execute('SELECT cursor FROM transport_cursors WHERE agent_id=? AND session_id=?',(agent_id,session_id)).fetchone()
            return row[0] if row else 0

    def ingest_transport(self,agent_id,session_id,events):
        prepared=[]
        for event in events:
            event=dict(event)
            payload=event.get('payload')
            if isinstance(payload,dict) and isinstance(payload.get('source'),str):
                event['payload']={**payload,'source_ref':self.put_artifact(payload['source'])}
                event['payload'].pop('source')
            prepared.append(event)
        with self.lock,self.db:
            committed=self.transport_cursor(agent_id,session_id)
            for event in prepared:
                cursor=event['cursor']
                if cursor<=committed:
                    continue
                if cursor!=committed+1:
                    raise TransportGapError(f'transport gap for {agent_id}: expected {committed+1}, got {cursor}')
                if event.get('session_id',session_id)!=session_id:
                    raise TransportGapError('mixed session events in one batch')
                if event.get('run_id')!=self.run_id:
                    self._insert('prior_run_runtime_event',event,actor_id=agent_id,source='orphaned_runtime')
                else:
                    self._insert(event.get('kind','bot_event'),event,actor_id=agent_id,source='agent_claim' if event.get('source')=='agent_claim' else 'bot_runtime',world_epoch=event.get('world_epoch',0),runtime_epoch=event.get('runtime_epoch') or 0,execution_id=event.get('execution_id'))
                    kind,payload=event.get('kind'),event.get('payload',{})
                    if isinstance(payload,dict) and isinstance(payload.get('record'),dict) and payload['record'].get('execution_id'):
                        self._save_execution(agent_id,payload['record'])
                    if kind=='bot_chat':
                        self._insert('player_event',{**event,'kind':'chat'},actor_id=agent_id,world_epoch=event.get('world_epoch',0))
                    elif kind=='runtime_reset' or (kind=='runtime_ready' and isinstance(payload,dict) and payload.get('globals_lost')):
                        self._insert('player_event',{**event,'kind':'runtime_reset'},actor_id=agent_id,world_epoch=event.get('world_epoch',0))
                committed=cursor
            self.db.execute('INSERT INTO transport_cursors VALUES (?,?,?) ON CONFLICT(agent_id,session_id) DO UPDATE SET cursor=excluded.cursor',(agent_id,session_id,committed))
            return committed

    def apply_restore_transition(self, receipt_id, payload, agent_ids):
        with self.lock,self.db:
            if self.get_state('last_applied_restore_receipt') == receipt_id:
                return self.get_state('world_epoch',0)
            epoch = self.get_state('world_epoch',0) + 1
            self._set_state('world_epoch',epoch)
            self._set_state('last_applied_restore_receipt',receipt_id)
            self._insert('world_restored',payload,world_epoch=epoch)
            for agent in agent_ids:
                self._insert('player_event',{'kind':'world_restored','world_epoch':epoch,'runtime_epoch':0,'payload':payload},actor_id=agent,world_epoch=epoch)
            return epoch

    def claim_control(self):
        with self.lock,self.db:
            row=self.db.execute("SELECT * FROM controls WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return None
            self.db.execute("UPDATE controls SET state='processing' WHERE control_id=?",(row['control_id'],))
            return {**dict(row),'arguments':json.loads(row['arguments_json'])}

    def finish_control(self,control_id,state,result):
        with self.lock,self.db:
            self.db.execute('UPDATE controls SET state=?,result_json=? WHERE control_id=?',(state,self._json(result),control_id))

    def interrupt_controls(self):
        with self.lock,self.db:
            self.db.execute("UPDATE controls SET state='interrupted',result_json=? WHERE state='processing'",(self._json({'reason':'supervisor restarted; side effects were not replayed'}),))

    @staticmethod
    def _file_sizes(directory):
        # scandir yields entries lazily; Path.rglob internally materializes each
        # directory, causing increasingly large allocation spikes in artifact stores.
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        yield from Recorder._file_sizes(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        yield entry.stat(follow_symlinks=False).st_size
                except FileNotFoundError:
                    continue

    def storage_status(self,full=False):
        if full:
            total = sum(self._file_sizes(self.run_dir))
        else:
            total = self.artifact_bytes
            with os.scandir(self.run_dir) as entries:
                for entry in entries:
                    try:
                        if entry.name.startswith('events.sqlite3') and entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except FileNotFoundError:
                        continue
        free=shutil.disk_usage(self.run_dir).free
        return {'measured_run_bytes':total,'artifact_bytes':self.artifact_bytes,'free_disk_bytes':free,'limit_exceeded':total>self.limits.get('max_run_bytes',24*1024**3) or free<self.limits.get('min_free_disk_bytes',256*1024**2)}

    def export_jsonl(self):
        target=self.run_dir/'events.jsonl'
        descriptor,temporary=tempfile.mkstemp(prefix='.events-',dir=self.run_dir)
        try:
            with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
                for event in self.all_events():
                    stream.write(self._json(event)+'\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary,target)
            fsync_directory(self.run_dir)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return target

    def close(self):
        with self.lock:
            if not self.closed:
                self.db.close()
                self.closed=True

def enqueue_control(run_dir,command,arguments):
    control_id=uuid.uuid4().hex
    with closing(sqlite3.connect(Path(run_dir)/'events.sqlite3',timeout=10)) as connection, connection:
        connection.execute('INSERT INTO controls VALUES (?,?,?,?,?,NULL)',(control_id,command,json.dumps(arguments,allow_nan=False),'queued',datetime.now(timezone.utc).isoformat()))
    return control_id
