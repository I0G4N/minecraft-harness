"""Local operator timeline. Player code never receives this listener or its token."""
from __future__ import annotations

import hashlib
import html
import codecs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import os
import stat
import threading
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import parse_qs, unquote, urlsplit

from .dashboard_catalog import RunCatalog
from .world_render import WorldRenderError, render_world, world_fingerprint

_WORLD_IMAGE_LOCKS = {}
_WORLD_IMAGE_LOCK = threading.Lock()


def timeline(run_dir: Path, after=0, limit=50, actor=None):
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Invalid timeline page')
    query = 'SELECT * FROM events WHERE event_seq>?'
    arguments = [after]
    if actor:
        query += ' AND actor_id=?'
        arguments.append(actor)
    query += ' ORDER BY event_seq LIMIT ?'
    arguments.append(limit)
    connection = sqlite3.connect((Path(run_dir) / 'events.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    events, size = [], 0
    try:
        for row in connection.execute(query, arguments):
            event = dict(row)
            raw = event.pop('payload_json')
            event['payload'] = json.loads(raw) if len(raw) <= 16000 else {'preview': raw[:16000], 'truncated': True}
            encoded = json.dumps(event, ensure_ascii=False)
            if events and size + len(encoded.encode()) > 256000:
                break
            events.append(event)
            size += len(encoded.encode())
    finally:
        connection.close()
    return {'events': events, 'next_cursor': events[-1]['event_seq'] if events else after}


def artifact_page(run_dir, digest, offset=0, limit=32000):
    if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest) or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 64000:
        raise ValueError('Invalid artifact page')
    directory = Path(run_dir) / 'artifacts'
    path = directory / digest
    if directory.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError('Artifact unavailable')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Artifact unavailable')
        check = hashlib.sha256()
        while chunk := stream.read(128000):
            check.update(chunk)
        if check.hexdigest() != digest:
            raise ValueError('Artifact checksum mismatch')
        size = os.fstat(stream.fileno()).st_size
        stream.seek(offset)
        raw = stream.read(limit + 4)
    # Page boundaries must not replace a split Chinese character or emoji.
    # A very small page may extend by at most three bytes to make progress.
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    content = decoder.decode(raw[:limit], final=offset + len(raw[:limit]) >= size)
    pending = decoder.getstate()[0]
    consumed = len(raw[:limit]) - len(pending)
    if not consumed and pending:
        fed = len(raw[:limit])
        for byte in raw[limit:]:
            fed += 1
            content += decoder.decode(bytes([byte]), final=False)
            if content:
                consumed = fed - len(decoder.getstate()[0])
                break
    return {'sha256': digest, 'offset': offset, 'content': content,
            'next_offset': offset + consumed if offset + consumed < size else None,
            'size_bytes': size, 'offset_unit': 'utf8_bytes'}


def world_image(run_dir: Path, style: str = "mesh") -> tuple[bytes, dict]:
    """Render/cache a read-only block map from the run's configured server save."""
    manifest_path = Path(run_dir) / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    config = manifest.get("config", {}) if isinstance(manifest, dict) else {}
    server = config.get("server", {}) if isinstance(config, dict) else {}
    raw = server.get("world_dir")
    if not raw:
        raise ValueError("run has no server world_dir")
    path = Path(raw)
    if not path.is_absolute():
        candidates = [Path(__file__).resolve().parents[2] / path, Path.cwd() / path, Path(run_dir) / path]
        path = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    reports = Path(run_dir) / "reports"
    if reports.is_symlink():
        raise ValueError("report cache must not be a symlink")
    reports.mkdir(parents=True, exist_ok=True)
    suffix = "_160" if style == "mesh" else ""
    png, meta_path = reports / f"world_render_{style}{suffix}.png", reports / f"world_render_{style}{suffix}.json"
    with _WORLD_IMAGE_LOCK:
        lock = _WORLD_IMAGE_LOCKS.setdefault((str(Path(run_dir).resolve()), style), threading.Lock())
    with lock:
        fingerprint = world_fingerprint(path)
        cached = {}
        if meta_path.is_file() and png.is_file():
            try:
                cached = json.loads(meta_path.read_text())
            except (OSError, ValueError):
                cached = {}
        if cached.get("input_fingerprint", {}).get("sha256") != fingerprint.get("sha256") or not png.is_file():
            metadata = render_world(path, png, style=style, width=160 if style == 'mesh' else 64,
                                    depth=160 if style == 'mesh' else 64, full_world=False)
            meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
            return png.read_bytes(), metadata
        return png.read_bytes(), cached


def _asset(name):
    return files('minecraft_harness').joinpath('static', name).read_bytes()


class _DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, *args, **kwargs):
        self._connections = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        if not self._connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self._connections.release()


def create_dashboard(run_dir, port, status_reader, control_writer):
    catalog = RunCatalog(run_dir)
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, format, *args):
            pass

        def _local_request(self):
            try:
                values = self.headers.get_all('Host', [])
                if len(values) != 1 or len(self.path) > 8192:
                    return False
                host = urlsplit('http://' + values[0])
                return (host.hostname in {'127.0.0.1', 'localhost'} and host.port == self.server.server_port
                        and not host.username and not host.password and not host.path and not host.query and not host.fragment)
            except ValueError:
                return False

        def send_json(self, value, code=200, compress=False):
            encoded = json.dumps(value, ensure_ascii=False).encode()
            headers = {}
            if len(encoded) > 512000 and not compress:
                encoded = b'{"error":"response exceeds page limit; request bounded evidence pages"}'
                code = 413
            elif compress and len(encoded) > 262144:
                import gzip
                encoded = gzip.compress(encoded, compresslevel=6)
                headers['Content-Encoding'] = 'gzip'
            self.send_bytes(encoded, 'application/json', code, extra_headers=headers)

        def send_bytes(self, raw, content_type, code=200, *, download=None, extra_headers=None):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            for header, value in (extra_headers or {}).items():
                self.send_header(header, value)
            # frame-src allows the optional live viewer iframes: same operator
            # host and scheme but a different loopback port (http: covers it).
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-src 'self' http:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            if download:
                self.send_header('Content-Disposition', f'attachment; filename="{download}"')
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Closing a tab or changing a filter cancels in-flight reads.

        def do_GET(self):
            if not self._local_request():
                self.send_json({'error': 'local operator Host required'}, 403)
                return
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            try:
                def value(name, default=None):
                    values = query.get(name)
                    if values is None:
                        return default
                    if len(values) != 1:
                        raise ValueError('Repeated query parameter')
                    return values[0]

                def integer(name, default=None):
                    item = value(name)
                    return default if item is None else int(item)

                if parsed.path == '/':
                    page = _asset('dashboard.html').decode('utf-8').replace('__TOKEN__', html.escape(token, quote=True))
                    self.send_bytes(page.encode(), 'text/html; charset=utf-8')
                    return
                if parsed.path in {'/dashboard.css', '/dashboard.js'}:
                    self.send_bytes(_asset(parsed.path[1:]), 'text/css; charset=utf-8' if parsed.path.endswith('.css') else 'text/javascript; charset=utf-8')
                    return
                if parsed.path.startswith('/ui-assets/'):
                    # Minecraft-styled dashboard chrome: Pixel Perfection textures and
                    # the Cubic 11 pixel font (CC BY-SA 4.0 / SIL OFL 1.1).
                    name = unquote(parsed.path.removeprefix('/ui-assets/'))
                    if not re.fullmatch(r'(?:[A-Za-z0-9_.-]{1,80}\.(?:png|md)|fonts/[A-Za-z0-9_.-]{1,80}\.woff2)', name):
                        raise ValueError('Invalid UI asset')
                    directory = Path(__file__).resolve().parent / 'static' / 'ui-assets'
                    target = directory / name
                    if directory.is_symlink() or target.is_symlink() or not target.is_file():
                        raise ValueError('UI asset unavailable')
                    if name.endswith('.png'):
                        content_type = 'image/png'
                    elif name.endswith('.woff2'):
                        content_type = 'font/woff2'
                    else:
                        content_type = 'text/markdown; charset=utf-8'
                    self.send_bytes(target.read_bytes(), content_type)
                    return
                if parsed.path.startswith('/figure-assets/'):
                    # Pixel Perfection Legacy item/player textures (CC BY-SA 4.0),
                    # served only by this local operator dashboard.
                    name = unquote(parsed.path.removeprefix('/figure-assets/'))
                    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}\.(png|md)', name):
                        raise ValueError('Invalid figure asset')
                    directory = Path(__file__).resolve().parent / 'static' / 'figure-assets'
                    target = directory / name
                    if directory.is_symlink() or target.is_symlink() or not target.is_file():
                        raise ValueError('Figure asset unavailable')
                    self.send_bytes(target.read_bytes(), 'image/png' if name.endswith('.png') else 'text/markdown; charset=utf-8')
                    return
                if parsed.path in {'/favicon.ico', '/favicon.svg'}:
                    self.send_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#28594b"/><path d="M8 23V9h4l4 7 4-7h4v14h-4v-8l-4 6-4-6v8z" fill="#fff"/></svg>', 'image/svg+xml')
                    return
                if parsed.path == '/api/runs':
                    page = catalog.list(cursor=integer('cursor', 0), limit=integer('limit', 100))
                    selected = value('run')
                    if selected:
                        page['selected_run_id'] = catalog.resolve(selected).name
                    self.send_json(page)
                    return
                if parsed.path == '/api/world-image':
                    run_path = catalog.resolve(value('run'))
                    style = value('style', 'mesh')
                    if style not in {'top', 'iso', 'mesh'}:
                        raise ValueError('unsupported world style')
                    raw, metadata = world_image(run_path, style)
                    self.send_bytes(raw, 'image/png')
                    return
                path = catalog.resolve(value('run'))
                if parsed.path == '/api/status':
                    self.send_json({**status_reader(path), 'selected_run_id': path.name})
                elif parsed.path.startswith('/api/artifact/'):
                    self.send_json(artifact_page(path, parsed.path.removeprefix('/api/artifact/'), integer('offset', 0), integer('limit', 32000)))
                elif parsed.path in {'/download/report.json', '/download/report.md'}:
                    name = parsed.path.removeprefix('/download/')
                    target = path / 'reports' / name
                    if target.parent.is_symlink() or target.is_symlink():
                        raise ValueError('Report unavailable')
                    descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(descriptor, 'rb') as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            raise ValueError('Report unavailable')
                        raw = stream.read(16 * 1024 ** 2 + 1)
                    if len(raw) > 16 * 1024 ** 2:
                        self.send_json({'error': 'report exceeds download limit; use paged report API'}, 413)
                        return
                    self.send_bytes(raw, 'application/json; charset=utf-8' if name.endswith('.json') else 'text/markdown; charset=utf-8', download=name)
                else:
                    from . import dashboard_data as data
                    if parsed.path == '/api/events':
                        recent = value('recent', 'true')
                        if recent not in {'true', 'false', '1', '0'}:
                            raise ValueError('Invalid recent flag')
                        result = data.events_page(path, after=integer('after'), before=integer('before'),
                            recent=recent in {'true', '1'}, limit=integer('limit', 50), category=value('category', 'all'),
                            actor=value('actor'), kind=value('kind'), severity=value('severity'), q=value('q'),
                            start=value('start'), end=value('end'))
                    elif re.fullmatch(r'/api/event/[1-9][0-9]*', parsed.path):
                        result = data.event_detail(path, int(parsed.path.rsplit('/', 1)[1]), offset=integer('offset', 0), limit=integer('limit', 32000))
                    elif parsed.path == '/api/overview':
                        result = data.overview(path)
                    elif parsed.path == '/api/programs':
                        result = data.programs_page(path, cursor=value('cursor'), limit=integer('limit', 50), actor=value('actor'), status=value('status'), q=value('q'))
                    elif parsed.path.startswith('/api/programs/'):
                        identity = unquote(parsed.path.removeprefix('/api/programs/'))
                        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,512}', identity):
                            raise ValueError('Invalid program identity')
                        result = data.program_detail(path, identity, actor=value('actor'))
                    elif parsed.path == '/api/jobs':
                        result = data.jobs_page(path, cursor=value('cursor'), limit=integer('limit', 50), actor=value('actor'), status=value('status'), q=value('q'))
                    elif parsed.path == '/api/chat':
                        result = data.chat_page(path, cursor=value('cursor'), limit=integer('limit', 50), actor=value('actor'), q=value('q'))
                    elif parsed.path == '/api/observer':
                        result = data.observer_page(path, cursor=value('cursor'), limit=integer('limit', 30))
                    elif parsed.path == '/api/resources':
                        result = data.resources_page(path, cursor=value('cursor'), limit=integer('limit', 100))
                    elif parsed.path == '/api/report':
                        result = data.report_page(path, format=value('format', 'json'), offset=integer('offset', 0), limit=integer('limit', 32000))
                    elif parsed.path == '/api/figures':
                        from .figures import figures_for_dashboard
                        result = figures_for_dashboard(path)
                        self.send_json(result, compress=True)
                        return
                    else:
                        self.send_json({'error': 'not found'}, 404)
                        return
                    self.send_json(result)
            except (ValueError, OSError, sqlite3.Error, OverflowError, KeyError):
                self.send_json({'error': 'requested evidence unavailable or invalid'}, 400)

        def do_POST(self):
            origins = {f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'}
            if (not self._local_request() or self.headers.get('Origin') not in (None, *origins)):
                self.send_json({'error': 'local operator origin required'}, 403)
                return
            supplied = self.headers.get('X-Control-Token', '')
            if (self.path != '/api/control' or not supplied.isascii() or len(supplied) > 512
                    or not secrets.compare_digest(supplied, token)):
                self.send_json({'error': 'operator token required'}, 403)
                return
            try:
                if len(self.headers.get_all('Content-Length', [])) != 1 or self.headers.get('Transfer-Encoding'):
                    raise ValueError('Invalid request framing')
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 4096 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError('Invalid request')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('Control request must be an object')
                command = body.get('command')
                if command not in {'pause', 'freeze', 'resume', 'checkpoint', 'stop'}:
                    raise ValueError('Unsupported command')
                if set(body) - {'command', 'run'}:
                    raise ValueError('Unsupported control field')
                path = catalog.resolve(body.get('run'))
                self.send_json({'control_id': control_writer(path, command), 'command': command,
                                'run': path.name, 'state': 'queued'}, 202)
            except (ValueError, RuntimeError, TypeError, OSError):
                self.send_json({'error': 'command rejected; check Supervisor status'}, 400)
    return _DashboardServer(('127.0.0.1', port), Handler)


def serve_dashboard(run_dir, port, status_reader, control_writer):
    server = create_dashboard(run_dir, port, status_reader, control_writer)
    try:
        print(f'Operator dashboard: http://127.0.0.1:{server.server_port}', flush=True)
        server.serve_forever()
    finally:
        server.server_close()
