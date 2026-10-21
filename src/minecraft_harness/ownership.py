"""Host-local exclusive ownership of runtime endpoints and managed worlds."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from urllib.parse import urlparse


def _runtime_identity(url):
    parsed = urlparse(url.rstrip('/'))
    host = (parsed.hostname or '').lower()
    if host in {'127.0.0.1', '::1', 'localhost'}:
        host = 'localhost'
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    return f'{parsed.scheme.lower()}://{host}:{port}{parsed.path.rstrip("/")}'


class RuntimeOwnership:
    def __init__(self, runtime, server=None):
        self.urls = sorted({_runtime_identity(url) for url in [runtime.get('url'), *runtime.get('urls', {}).values()] if url})
        self.resources = {'runtime:' + url for url in self.urls}
        server = server or {}
        if server.get('world_dir'):
            self.resources.add('world-data:' + str(Path(server['world_dir']).expanduser().parent.resolve()))
        if server.get('managed_container'):
            self.resources.add('container:' + server['managed_container'])
        self.handles = []
        self.owned = False

    def acquire(self):
        if self.owned:
            return
        directory = Path(tempfile.gettempdir()) / f'minecraft-harness-locks-{os.getuid()}'
        directory.mkdir(mode=0o700, exist_ok=True)
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or directory.is_symlink() or metadata.st_uid != os.getuid():
            raise RuntimeError('runtime ownership directory is not a private operator-owned directory')
        directory.chmod(0o700)
        try:
            # Stable ordering prevents inconsistent partial acquisition when
            # experiments share more than one runtime or world resource.
            for resource in sorted(self.resources):
                path = directory / (hashlib.sha256(resource.encode()).hexdigest() + '.lock')
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                handle = os.fdopen(descriptor, 'r+')
                self.handles.append(handle)
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                handle.seek(0)
                handle.truncate()
                json.dump({'pid': os.getpid(), 'resource_type': resource.split(':', 1)[0]}, handle)
                handle.flush()
        except BlockingIOError:
            self.close()
            raise RuntimeError('an external bot runtime or Minecraft world/container is already owned by another local experiment') from None
        except BaseException:
            self.close()
            raise
        self.owned = True

    def close(self):
        for handle in reversed(self.handles):
            handle.close()
        self.handles = []
        self.owned = False
