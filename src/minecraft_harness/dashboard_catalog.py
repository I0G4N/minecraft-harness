"""Read-only discovery for a single run or an explicitly selected runs folder."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time


def _regular(path):
    try:
        return not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)
    except OSError:
        return False


def _document(path, maximum=2 * 1024 ** 2):
    if not _regular(path) or path.stat().st_size > maximum:
        return {}
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as handle:
        value = json.loads(handle.read(maximum + 1))
    return value if isinstance(value, dict) else {}


def _active(path):
    lock = path / '.supervisor.lock'
    if not _regular(lock):
        return False
    descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(descriptor)


class RunCatalog:
    """The caller chooses the entire discovery scope; HTTP paths never expand it."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        if not self.path.is_dir():
            raise ValueError('Dashboard requires an existing run or runs directory')
        self.single = _regular(self.path / 'events.sqlite3')
        self._cache = None
        self._until = 0
        self._lock = threading.Lock()

    def _paths(self):
        if self.single:
            return [self.path], False
        paths = []
        examined = 0
        with os.scandir(self.path) as entries:
            for entry in entries:
                examined += 1
                if examined > 5000:
                    return paths, True
                if entry.name.startswith('.') or not entry.is_dir(follow_symlinks=False):
                    continue
                candidate = self.path / entry.name
                if _regular(candidate / 'events.sqlite3') and _regular(candidate / 'manifest.json'):
                    paths.append(candidate)
        return paths, False

    @staticmethod
    def _summary(path):
        manifest = _document(path / 'manifest.json')
        config = manifest.get('config') or {}
        if not isinstance(config, dict):
            config = {}
        experiment = config.get('experiment') or {}
        model = config.get('model') or {}
        agents = config.get('agents') or {}
        state = {}
        connection = sqlite3.connect((path / 'events.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=.3)
        try:
            connection.execute('PRAGMA query_only=ON')
            for key, encoded in connection.execute("SELECT key,value_json FROM state WHERE key IN ('status','active_wall_seconds')"):
                if len(encoded) < 10000:
                    state[key] = json.loads(encoded)
            count = connection.execute('SELECT COALESCE(MAX(event_seq),0) FROM events').fetchone()[0]
        finally:
            connection.close()
        created = manifest.get('created_at')
        if not isinstance(created, str):
            created = datetime.fromtimestamp((path / 'events.sqlite3').stat().st_mtime, timezone.utc).isoformat()
        diagnostic = manifest.get('diagnostic_only')
        if type(diagnostic) is not bool:
            diagnostic = (config.get('runtime', {}).get('mode') == 'fake' or model.get('kind') == 'fake') if config else None
        return {'id': path.name, 'name': str(experiment.get('name') or path.name)[:300],
                'status': state.get('status', 'unknown'), 'active': _active(path),
                'created_at': created, 'profile': experiment.get('profile', 'strict') if config else None,
                'diagnostic_only': diagnostic, 'players': agents.get('ids', []),
                'goal': str(experiment.get('goal') or '')[:2000], 'task': experiment.get('task'),
                'model': model.get('model') or model.get('kind'), 'event_count': count,
                'active_wall_seconds': state.get('active_wall_seconds')}

    def list(self, *, cursor=0, limit=100):
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid run page')
        with self._lock:
            if self._cache is None or time.monotonic() >= self._until:
                paths, truncated = self._paths()
                runs, unavailable = [], 0
                deadline = time.monotonic() + 2
                # Discovery is bounded independently from the HTTP page size.
                for path in paths[:1000]:
                    if time.monotonic() >= deadline:
                        truncated = True
                        break
                    try:
                        runs.append(self._summary(path))
                    except (OSError, ValueError, sqlite3.Error, TypeError, AttributeError):
                        unavailable += 1
                runs.sort(key=lambda run: (run['active'], run['created_at'], run['id']), reverse=True)
                self._cache = runs, truncated or len(paths) > 1000, unavailable
                self._until = time.monotonic() + 2
            runs, truncated, unavailable = self._cache
            page = runs[cursor:cursor + limit]
            return {'runs': page, 'selected_run_id': runs[0]['id'] if runs else None,
                    'catalog_mode': 'single' if self.single else 'collection',
                    'has_more': cursor + len(page) < len(runs),
                    'next_cursor': cursor + len(page) if cursor + len(page) < len(runs) else None,
                    'total': len(runs), 'discovery_truncated': truncated, 'unavailable_runs': unavailable,
                    'scope': 'explicit_directory_direct_children', 'read_only': True}

    def resolve(self, identity=None):
        if identity is None or identity == '':
            page = self.list(limit=1)
            identity = page['selected_run_id']
        if (not isinstance(identity, str) or not identity or len(identity.encode()) > 255
                or identity in {'.', '..'} or '/' in identity or '\\' in identity or '\0' in identity):
            raise ValueError('Select an available experiment')
        if self.single:
            if identity != self.path.name:
                raise ValueError('Experiment is outside the selected scope')
            return self.path
        path = self.path / identity
        if path.is_symlink() or not path.is_dir() or path.resolve().parent != self.path:
            raise ValueError('Experiment is outside the selected scope')
        if not _regular(path / 'events.sqlite3') or not _regular(path / 'manifest.json'):
            raise ValueError('Experiment is not available')
        return path
