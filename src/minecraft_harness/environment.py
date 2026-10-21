"""Operator-only inspection of the actual Vanilla world, never player observations."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import stat
import secrets
import struct
from pathlib import Path

from .recorder import atomic_json
from .deployment import capture_deployment


# Explicit gameplay/server declaration; credential and arbitrary URL/text fields
# (rcon.password, resource-pack, text-filtering-config, motd) never enter manifests.
SERVER_PROPERTIES_WHITELIST = frozenset({
    'allow-flight', 'allow-nether', 'broadcast-console-to-ops', 'broadcast-rcon-to-ops',
    'difficulty', 'enable-command-block', 'enable-jmx-monitoring', 'enable-query', 'enable-rcon',
    'enable-status', 'enforce-secure-profile', 'enforce-whitelist', 'entity-broadcast-range-percentage',
    'force-gamemode', 'function-permission-level', 'gamemode', 'generate-structures', 'generator-settings',
    'hardcore', 'hide-online-players', 'initial-disabled-packs', 'initial-enabled-packs', 'level-name',
    'level-seed', 'level-type', 'max-chained-neighbor-updates', 'max-players', 'max-tick-time',
    'max-world-size', 'network-compression-threshold', 'online-mode', 'op-permission-level',
    'player-idle-timeout', 'prevent-proxy-connections', 'pvp', 'query.port', 'rate-limit',
    'require-resource-pack', 'server-ip', 'server-port', 'simulation-distance', 'spawn-animals',
    'spawn-monsters', 'spawn-npcs', 'spawn-protection', 'sync-chunk-writes', 'use-native-transport',
    'view-distance', 'white-list',
})


class _NBT:
    def __init__(self, raw: bytes):
        self.stream = io.BytesIO(raw)
        self.items = 0

    def read(self, size):
        value = self.stream.read(size)
        if len(value) != size:
            raise ValueError("Truncated level.dat NBT")
        return value

    def number(self, fmt):
        return struct.unpack(fmt, self.read(struct.calcsize(fmt)))[0]

    def text(self):
        return self.read(self.number('>H')).decode('utf-8')

    def value(self, kind, depth=0):
        self.items += 1
        if depth > 64 or self.items > 1000000:
            raise ValueError("World NBT exceeds structural limits")
        if kind in {1, 2, 3, 4, 5, 6}:
            return self.number({1: '>b', 2: '>h', 3: '>i', 4: '>q', 5: '>f', 6: '>d'}[kind])
        if kind == 8:
            return self.text()
        if kind == 10:
            result = {}
            while (tag := self.number('>B')) != 0:
                name = self.text()
                if name in result:
                    raise ValueError("Duplicate NBT field")
                result[name] = self.value(tag, depth + 1)
            return result
        if kind in {7, 9, 11, 12}:
            element = self.number('>B') if kind == 9 else {7: 1, 11: 3, 12: 4}[kind]
            count = self.number('>i')
            if not 0 <= count <= 1000000:
                raise ValueError("Invalid NBT array length")
            return [self.value(element, depth + 1) for _ in range(count)]
        raise ValueError("Unsupported NBT type")


def _regular_file(path: Path, maximum: int):
    """Nonblocking open rejects FIFOs/devices and leaf symlink races."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ValueError('Environment evidence must be a bounded regular file')
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise


def _read_regular(path: Path, maximum: int) -> bytes:
    with _regular_file(path, maximum) as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('Environment evidence exceeds its byte limit')
    return raw


def read_level(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError('level.dat must not be a symlink')
    with _regular_file(path, 16 * 1024 ** 2) as source, gzip.GzipFile(fileobj=source) as stream:
        raw = stream.read(16 * 1024 ** 2 + 1)
    if len(raw) > 16 * 1024 ** 2:
        raise ValueError("Decompressed level.dat exceeds 16 MiB")
    parser = _NBT(raw)
    if parser.number('>B') != 10:
        raise ValueError("Expected compound level.dat")
    parser.text()
    document = parser.value(10)
    if not isinstance(document.get('Data'), dict):
        raise ValueError("level.dat missing Data")
    return document['Data']


def read_properties(path: Path) -> dict[str, str]:
    if path.is_symlink():
        raise ValueError('server.properties must not be a symlink')
    result = {}
    for line in _read_regular(path, 1024 ** 2).decode('utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith(('#', '!')) and '=' in line:
            key, value = line.split('=', 1)
            result[key.strip()] = value.strip()
    return result


def inspect_environment(config: dict, *, run_id: str, resume=False) -> dict:
    """Validate before starting player sessions or billing any model call."""
    server, experiment = config['server'], config['experiment']
    if config['runtime']['mode'] == 'fake':
        return {'schema_version': 1, 'diagnostic_only': True, 'profile': experiment['profile'],
                'deviations': ['synthetic_game', 'no_actual_world'], 'verified': False}
    world = Path(server['world_dir']).resolve() if server.get('world_dir') else None
    paper = experiment['profile'] == 'paper_aligned'
    if not world or not (world / 'level.dat').is_file():
        if paper or server.get('require_fresh_world'):
            raise ValueError("Actual generated level.dat required before experiment admission")
        return {'schema_version': 1, 'verified': False, 'deviations': ['world_not_inspected']}
    data = read_level(world / 'level.dat')
    properties_path = Path(server.get('properties_path') or world.parent / 'server.properties')
    properties = read_properties(properties_path)
    seed = data.get('WorldGenSettings', {}).get('seed', data.get('RandomSeed'))
    if type(seed) is not int:
        raise ValueError("Actual world seed missing")
    expected = experiment.get('seed')
    if expected is not None and seed != expected:
        raise ValueError("Configured seed does not match actual world")
    game_rules = data.get('GameRules')
    if game_rules is not None and (not isinstance(game_rules, dict) or len(game_rules) > 256 or any(not isinstance(key, str) or len(key) > 256 or not isinstance(value, str) or len(value) > 4096 for key, value in game_rules.items())):
        raise ValueError('Malformed actual GameRules evidence')
    actual = {'game_rules': game_rules, 'seed': seed, 'version': data.get('Version', {}).get('Name'),
              'gamemode': data.get('GameType'), 'difficulty': data.get('Difficulty'),
              'hardcore': bool(data.get('hardcore')), 'allow_commands': bool(data.get('allowCommands'))}
    if actual['version'] != config['runtime']['minecraft_version']:
        raise ValueError("Actual world version does not match runtime")
    if paper and (actual['gamemode'] != 0 or actual['difficulty'] != 1 or actual['hardcore'] or actual['allow_commands']):
        raise ValueError("paper_aligned requires Survival/Easy with cheats disabled")
    if paper:
        for key, expected_value in {'gamemode': 'survival', 'difficulty': 'easy',
                                    'enable-command-block': 'false', 'enable-rcon': 'false',
                                    'op-permission-level': '0'}.items():
            if properties.get(key) != expected_value:
                raise ValueError(f"Actual server rule mismatch: {key}")
        operators = world.parent / 'ops.json'
        if operators.is_symlink():
            raise ValueError('Operator list must not be a symlink')
        if operators.exists() and json.loads(_read_regular(operators, 1024 ** 2)) != []:
            raise ValueError("Experiment players must not have operator privileges")
    marker = world.parent / '.harness-world.json'
    if marker.is_symlink():
        raise ValueError('World origin marker must not be a symlink')
    identity = json.loads(_read_regular(marker, 64000)) if marker.exists() else None
    if identity is not None and (not isinstance(identity, dict) or identity.get('schema_version') != 1
            or not isinstance(identity.get('world_id'), str) or not identity['world_id']
            or type(identity.get('seed')) is not int):
        raise ValueError('Malformed world origin marker')
    if paper and (not identity or identity.get('origin') not in {'new_random', 'new_seeded'}):
        raise ValueError('Paper trial requires a provisioned new-world origin marker')
    if identity and identity.get('run_id') not in {None, run_id}:
        raise ValueError("World already belongs to another experiment")
    if resume and server.get('require_fresh_world') and (not identity or identity.get('run_id') != run_id):
        raise ValueError("Cannot resume an unbound world")
    if not resume and server.get('require_fresh_world'):
        if identity and identity.get('run_id'):
            raise ValueError("Fresh experiment cannot reuse a previously bound world")
        if data.get('Player'):
            raise ValueError('Fresh experiment refuses an embedded single-player save')
        for category in ('playerdata', 'stats', 'advancements'):
            directory = world / category
            if directory.is_symlink():
                raise ValueError("Fresh experiment refuses symlinked player progress")
            if directory.exists() and any(directory.iterdir()):
                raise ValueError("Fresh experiment refuses existing player progress")
    if not identity:
        identity = {'schema_version': 1, 'world_id': secrets.token_hex(16), 'origin': 'operator_generated', 'seed': seed}
    if identity.get('seed') != seed:
        raise ValueError("World identity seed mismatch")
    identity['run_id'] = run_id
    atomic_json(marker, identity)
    return {'schema_version': 1, 'verified': True, 'profile': experiment['profile'],
            'world_id': identity['world_id'], 'origin': identity['origin'], 'actual': actual,
            'server_properties': {key: value for key, value in sorted(properties.items()) if key in SERVER_PROPERTIES_WHITELIST},
            'server_properties_whitelist_version': 1,
            'properties_sha256': hashlib.sha256(_read_regular(properties_path, 1024 ** 2)).hexdigest(),
            'level_dat_sha256': hashlib.sha256(_read_regular(world / 'level.dat', 16 * 1024 ** 2)).hexdigest(),
            'deviations': ['fixed_seed'] if paper and identity['origin'] != 'new_random' else [], 'operator_only': True}


def provision_world(data_dir: Path, *, seed: int | None = None, properties_template: Path | None = None) -> dict:
    """Prepare an empty trial; does not start Java or accept the EULA."""
    candidate = Path(data_dir)
    if candidate.is_symlink():
        raise ValueError('World destination must not be a symlink')
    data_dir = candidate.resolve()
    random_seed = seed is None
    if random_seed:
        seed = secrets.randbits(64) - 2 ** 63
    if type(seed) is not int or not -(2 ** 63) <= seed < 2 ** 63:
        raise ValueError("Seed must be a signed 64-bit integer")
    template = properties_template or Path(__file__).resolve().parents[2] / 'infra' / 'server.properties'
    if template.is_symlink() or not template.is_file() or template.stat().st_size > 1024 ** 2:
        raise ValueError('World properties template must be a bounded regular file')
    lines = [line for line in _read_regular(template, 1024 ** 2).decode('utf-8').splitlines() if not line.lstrip().startswith('level-seed=')]
    data_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    (data_dir / 'server.properties').write_text('\n'.join(lines) + f'\nlevel-seed={seed}\n')
    identity = {'schema_version': 1, 'world_id': secrets.token_hex(16), 'origin': 'new_random' if random_seed else 'new_seeded', 'seed': seed, 'run_id': None}
    atomic_json(data_dir / '.harness-world.json', identity)
    return identity
