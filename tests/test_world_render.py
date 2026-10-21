"""Unit tests for the read-only world block-map renderer.

The fixture builds a tiny synthetic Minecraft Java 1.20.4 world (level.dat plus
zlib/gzip-compressed region files) so the tests never depend on server data.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from minecraft_harness.world_mesh import (
    MAX_FULL_WORLD_CELLS,
    MESH_MAX_COLUMNS,
    _raster_triangle,
    block_material,
    decode_png,
    sampled_material_pixel,
    _mesh_extent,
    _visible_face,
)
from minecraft_harness.world_render import (
    MAX_CHUNKS,
    WorldRenderError,
    chunk_surface,
    color_for_block,
    decode_block_states,
    parse_chunk_nbt,
    render_world,
    world_fingerprint,
)


# ---------------------------------------------------------------------------
# Minimal NBT / Anvil fixture builders.

def _text(value: str) -> bytes:
    raw = value.encode('utf-8')
    return struct.pack('>H', len(raw)) + raw


def _tag(kind: int, name: str, payload: bytes) -> bytes:
    return bytes([kind]) + _text(name) + payload


def nbt_compound(fields) -> bytes:
    result = b''
    for name, (kind, payload) in fields.items():
        result += _tag(kind, name, payload)
    return result + b'\x00'


def nbt_string(value: str):
    return 8, _text(value)


def nbt_int(value: int):
    return 3, struct.pack('>i', value)


def nbt_byte(value: int):
    return 1, struct.pack('>b', value)


def nbt_long_array(values):
    values = [value & 0xFFFFFFFFFFFFFFFF for value in values]
    return 12, struct.pack('>i', len(values)) + b''.join(struct.pack('>Q', value) for value in values)


def nbt_list(element_kind: int, payloads):
    return 9, struct.pack('>Bi', element_kind, len(payloads)) + b''.join(payloads)


def packed_indices(indices, palette_size: int):
    """Encode 4096 palette indices with the 1.16+ compact (non-crossing) layout."""
    bits = max(4, (palette_size - 1).bit_length())
    per_long = 64 // bits
    longs = [0] * ((len(indices) + per_long - 1) // per_long)
    for position, index in enumerate(indices):
        longs[position // per_long] |= index << ((position % per_long) * bits)
    signed = [value - (1 << 64) if value >= (1 << 63) else value for value in longs]
    return tuple(signed)


def section_nbt(y: int, palette, indices=None) -> bytes:
    """One chunk section; ``indices`` is None for a single-palette section."""
    block_states = {'palette': nbt_list(10, [nbt_compound({'Name': nbt_string(name)}) for name in palette])}
    if indices is not None:
        block_states['data'] = nbt_long_array(packed_indices(indices, len(palette)))
    return nbt_compound({'Y': nbt_byte(y), 'block_states': (10, nbt_compound(block_states))})


def chunk_nbt(chunk_x: int, chunk_z: int, sections) -> bytes:
    return bytes([10]) + _text('') + nbt_compound({
        'DataVersion': nbt_int(3700),
        'xPos': nbt_int(chunk_x),
        'zPos': nbt_int(chunk_z),
        'Status': nbt_string('minecraft:full'),
        'sections': nbt_list(10, sections),
    })


def flat_chunk(chunk_x: int = 0, chunk_z: int = 0, *, surface='minecraft:grass_block',
               surface_y=32) -> bytes:
    """A chunk whose top block is ``surface`` at ``surface_y`` above a stone base."""
    section_y = surface_y // 16
    local_y = surface_y % 16
    top_indices = [0 if (index >> 8) == local_y else 1 for index in range(4096)]
    base_sections = [section_nbt(y, ['minecraft:stone'], None) for y in range(0, section_y)]
    top_section = section_nbt(section_y, [surface, 'minecraft:air', 'minecraft:dirt'], top_indices)
    air_sections = [section_nbt(y, ['minecraft:air'], None) for y in range(section_y + 1, 8)]
    return chunk_nbt(chunk_x, chunk_z, base_sections + [top_section] + air_sections)


def write_level_dat(world: Path, *, spawn=(8, 70, 8), level_name='fixture-world', seed=42) -> None:
    data = nbt_compound({
        'LevelName': nbt_string(level_name),
        'SpawnX': nbt_int(spawn[0]),
        'SpawnY': nbt_int(spawn[1]),
        'SpawnZ': nbt_int(spawn[2]),
        'DataVersion': nbt_int(3700),
        'Version': (10, nbt_compound({'Name': nbt_string('1.20.4')})),
        'WorldGenSettings': (10, nbt_compound({'seed': (4, struct.pack('>q', seed))})),
        'GameType': nbt_byte(0),
        'Difficulty': nbt_byte(1),
    })
    root = bytes([10]) + _text('') + nbt_compound({'Data': (10, data)})
    (world / 'level.dat').write_bytes(gzip.compress(root))


def write_region(world: Path, region_x: int, region_z: int, chunks, *, compression=2) -> Path:
    """Write a region file from ``{(chunk_x, chunk_z): nbt_bytes}``."""
    header = bytearray(8192)
    payloads = b''
    next_sector = 2
    for (chunk_x, chunk_z), raw in sorted(chunks.items()):
        if compression == 2:
            body = zlib.compress(raw)
            code = 2
        elif compression == 1:
            body = gzip.compress(raw)
            code = 1
        elif compression == 3:
            body = raw
            code = 3
        else:
            raise AssertionError('unsupported fixture compression')
        payload = struct.pack('>IB', len(body) + 1, code) + body
        sectors = (len(payload) + 4095) // 4096
        payloads += payload + b'\x00' * (sectors * 4096 - len(payload))
        index = ((chunk_x % 32) + (chunk_z % 32) * 32) * 4
        struct.pack_into('>I', header, index, (next_sector << 8) | sectors)
        next_sector += sectors
    region = world / 'region' / f'r.{region_x}.{region_z}.mca'
    region.write_bytes(bytes(header) + payloads)
    return region


def region_chunk_start(region: Path, header_index: int) -> int:
    location = struct.unpack_from('>I', region.read_bytes(), header_index * 4)[0]
    return (location >> 8) * 4096


class WorldRenderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.world = self.root / 'world'
        (self.world / 'region').mkdir(parents=True)
        write_level_dat(self.world)

    def render(self, output=None, **kwargs):
        return render_world(self.world, output or self.root / 'map.png', **kwargs)

    def region(self, name='r.0.0.mca') -> Path:
        return self.world / 'region' / name

    # -- colours and low-level parsing -------------------------------------

    def test_color_map_is_deterministic_and_covers_common_blocks(self):
        self.assertEqual(color_for_block('minecraft:grass_block'), (0x6F, 0xA8, 0x4C))
        self.assertEqual(color_for_block('minecraft:grass_block'), color_for_block('minecraft:grass_block'))
        self.assertEqual(color_for_block('minecraft:water'), (0x3D, 0x6B, 0xC4))
        self.assertEqual(color_for_block('minecraft:lava'), (0xE0, 0x5A, 0x1E))
        self.assertEqual(color_for_block('minecraft:obsidian'), (0x1A, 0x11, 0x28))
        self.assertEqual(color_for_block('minecraft:oak_leaves'), (0x4E, 0x7B, 0x2E))
        self.assertEqual(color_for_block('minecraft:oak_planks'), (0xB0, 0x8A, 0x55))
        self.assertEqual(color_for_block('minecraft:diamond_block'), (0x62, 0xE6, 0xD8))
        self.assertEqual(color_for_block('minecraft:red_wool'), (0x97, 0x25, 0x20))
        # Unknown modded blocks get a stable fallback colour instead of raising.
        first = color_for_block('minecraft:modded_widget_block')
        self.assertEqual(first, color_for_block('minecraft:modded_widget_block'))
        self.assertEqual(len(first), 3)
        with self.assertRaises(ValueError):
            color_for_block('')

    def test_packed_block_states_decode_round_trip(self):
        palette_size = 20
        indices = [(index * 7) % palette_size for index in range(4096)]
        signed = packed_indices(indices, palette_size)
        unsigned = tuple(value & 0xFFFFFFFFFFFFFFFF for value in signed)
        self.assertEqual(decode_block_states(unsigned, palette_size), indices)
        self.assertIsNone(decode_block_states((), 1))
        with self.assertRaises(ValueError):
            decode_block_states((), palette_size)
        with self.assertRaises(ValueError):
            decode_block_states((0,) * 10, 4097)

    def test_chunk_nbt_parsing_and_surface(self):
        write_region(self.world, 0, 0, {(0, 0): flat_chunk(0, 0)})
        region = self.region()
        start = region_chunk_start(region, 0)
        raw = region.read_bytes()
        length, compression = struct.unpack('>IB', raw[start:start + 5])
        self.assertEqual(compression, 2)
        payload = zlib.decompress(raw[start + 5:start + 4 + length])
        chunk = parse_chunk_nbt(payload)
        self.assertEqual((chunk['x'], chunk['z'], chunk['data_version']), (0, 0, 3700))
        self.assertEqual(chunk['status'], 'minecraft:full')
        self.assertEqual(len(chunk['sections']), 8)
        top, height, top_y = chunk_surface(chunk)
        self.assertEqual(len(top), 256)
        self.assertEqual(len(set(top)), 1)
        self.assertEqual(top[0], 'minecraft:grass_block')
        self.assertEqual((height[0], top_y[0]), (32, 32))
        with self.assertRaises(ValueError):
            parse_chunk_nbt(b'\x0a\x00\x00\x09')
        with self.assertRaises(ValueError):
            parse_chunk_nbt(nbt_compound({'xPos': nbt_int(0)}))

    def test_surface_ignores_decoration_and_leaf_canopy_height(self):
        # A single section: grass floor, decorative tall grass, then a leaf canopy.
        palette = ['minecraft:grass_block', 'minecraft:tall_grass', 'minecraft:oak_leaves',
                   'minecraft:oak_log', 'minecraft:air']
        indices = [4] * 4096
        for column in range(256):
            indices[column] = 0        # floor: grass at local y=0
            indices[256 + column] = 1  # local y=1: decorative tall grass
            indices[512 + column] = 2  # local y=2: leaf canopy
            indices[768 + column] = 3  # local y=3: oak log core
        chunk = parse_chunk_nbt(chunk_nbt(0, 0, [section_nbt(0, palette, indices)]))
        top, height, top_y = chunk_surface(chunk)
        self.assertEqual(top[0], 'minecraft:oak_log')
        self.assertEqual(top_y[0], 3)
        self.assertEqual(height[0], 3)  # logs are solid and lift the terrain height
        # Without logs the canopy keeps its leaf colour but not the terrain lift.
        indices = [4] * 4096
        for column in range(256):
            indices[column] = 0
            indices[256 + column] = 1
            indices[512 + column] = 2
        chunk = parse_chunk_nbt(chunk_nbt(0, 0, [section_nbt(0, palette, indices)]))
        top, height, top_y = chunk_surface(chunk)
        self.assertEqual(top[0], 'minecraft:oak_leaves')
        self.assertEqual(top_y[0], 2)
        self.assertEqual(height[0], 0)  # the grass floor defines terrain height

    # -- full render -------------------------------------------------------

    def test_render_writes_deterministic_png_with_metadata(self):
        write_region(self.world, 0, 0, {(0, 0): flat_chunk(0, 0)})
        output = self.root / 'map.png'
        before = self.region().read_bytes()
        metadata = self.render(output)
        data = output.read_bytes()
        self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
        width, height = struct.unpack('>II', data[16:24])
        self.assertEqual((width, height), (1024, 640))
        self.assertEqual(metadata['status'], 'partial')  # 24 of 25 chunks absent
        self.assertEqual(metadata['chunks'], {'requested': 25, 'read': 1, 'missing': 24, 'corrupt': 0})
        self.assertEqual(metadata['size'], [1024, 640])
        self.assertEqual(metadata['png_bytes'], len(data))
        self.assertEqual(metadata['png_sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(metadata['spawn'], {'x': 8, 'y': 70, 'z': 8})
        self.assertEqual(metadata['bounds'], {'x0': -24, 'x1': 40, 'z0': -24, 'z1': 40})
        self.assertEqual(metadata['style'], 'top_down')
        self.assertTrue(metadata['read_only'])
        self.assertTrue(metadata['world_files_unchanged'])
        self.assertEqual(metadata['block_counts'], {'minecraft:grass_block': 256})
        self.assertEqual(metadata['level_name'], 'fixture-world')
        self.assertEqual(metadata['height_frame'], {'min': 32, 'max': 32, 'span': 0,
                         'bar_ticks_every_blocks': 8, 'scale_bar_blocks': 16})
        self.assertIn('Colour', metadata['caption'])
        self.assertTrue(metadata['method'].startswith('Stdlib Anvil reader'))
        self.assertIn('input_fingerprint', metadata)
        # A second render is byte-for-byte deterministic and never touches the world.
        again = self.render(output)
        self.assertEqual(again['png_sha256'], metadata['png_sha256'])
        self.assertEqual(self.region().read_bytes(), before)

    def test_chunk_aligned_center_renders_fully_ok(self):
        write_region(self.world, 0, 0, {(0, 0): flat_chunk(0, 0)})
        metadata = self.render(self.root / 'map.png', center=(8, 8), width=16, depth=16)
        self.assertEqual(metadata['status'], 'ok')
        self.assertEqual(metadata['chunks'], {'requested': 1, 'read': 1, 'missing': 0, 'corrupt': 0})
        self.assertEqual(metadata['bounds'], {'x0': 0, 'x1': 16, 'z0': 0, 'z1': 16})
        self.assertEqual(metadata['block_counts'], {'minecraft:grass_block': 256})
        self.assertEqual(metadata['png_bytes'], len((self.root / 'map.png').read_bytes()))

    def test_partial_missing_and_corrupt_chunks_are_reported(self):
        write_region(self.world, 0, 0, {(0, 0): flat_chunk(0, 0), (1, 0): flat_chunk(1, 0)})
        metadata = self.render(self.root / 'partial.png', center=(8, 8), width=32, depth=16)
        self.assertEqual(metadata['status'], 'partial')
        self.assertEqual(metadata['chunks'], {'requested': 3, 'read': 2, 'missing': 1, 'corrupt': 0})
        # Corrupt only chunk (1,0): flip its compression id to unsupported LZ4.
        region = self.region()
        raw = bytearray(region.read_bytes())
        start = region_chunk_start(region, 1)
        raw[start + 4] = 4
        region.write_bytes(bytes(raw))
        metadata = self.render(self.root / 'corrupt.png', center=(8, 8), width=32, depth=16)
        self.assertEqual(metadata['status'], 'partial')
        self.assertEqual(metadata['chunks']['read'], 1)
        self.assertEqual(metadata['chunks']['corrupt'], 1)
        self.assertTrue(any('LZ4' in message for message in metadata['errors']))

    def test_missing_world_and_symlinks_are_rejected(self):
        output = self.root / 'map.png'
        with self.assertRaises(WorldRenderError):
            render_world(self.world / 'absent', output)
        link = self.root / 'world-link'
        os.symlink(self.world, link)
        with self.assertRaises(WorldRenderError):
            render_world(link, output)
        write_region(self.world, 0, 0, {(0, 0): flat_chunk(0, 0)})
        target = self.root / 'target.png'
        target.write_bytes(b'keep')
        os.symlink(target, output)
        with self.assertRaises(WorldRenderError):
            render_world(self.world, output)
        self.assertEqual(target.read_bytes(), b'keep')
        # A symlinked region file is refused rather than followed.
        region = self.region()
        moved = self.root / 'moved.mca'
        moved.write_bytes(region.read_bytes())
        region.unlink()
        os.symlink(moved, region)
        with self.assertRaises(WorldRenderError):
            render_world(self.world, self.root / 'other.png')

    def test_argument_bounds_and_output_parent_creation(self):
        write_region(self.world, 0, 0, {(0, 0): flat_chunk(0, 0)})
        with self.assertRaises(WorldRenderError):
            self.render(width=0)
        with self.assertRaises(WorldRenderError):
            self.render(style='isometric')
        with self.assertRaises(WorldRenderError):
            self.render(size=(10, 10))
        with self.assertRaises(WorldRenderError):
            self.render(center=(True, 0))
        with self.assertRaises(WorldRenderError):
            self.render(center=(0, 0, 0))
        with self.assertRaises(WorldRenderError):
            render_world(self.world, self.root / 'map.png', center=(0, 0), width=1040, depth=1040)
        nested = self.root / 'nested' / 'deeper' / 'map.png'
        metadata = self.render(nested, center=(8, 8), width=16, depth=16)
        self.assertTrue(nested.is_file())
        self.assertEqual(metadata['output'], str(nested))
        with self.assertRaises(WorldRenderError):
            render_world(self.root / 'world', self.root)  # output path is a directory

    def test_chunk_compression_variants(self):
        write_region(self.world, 0, 0, {(0, 0): flat_chunk(0, 0)}, compression=1)
        metadata = self.render(self.root / 'gzip.png', center=(8, 8), width=16, depth=16)
        self.assertEqual(metadata['chunks']['read'], 1)
        self.assertEqual(metadata['status'], 'ok')
        write_region(self.world, 0, 0, {(0, 0): flat_chunk(0, 0)}, compression=3)
        metadata = self.render(self.root / 'raw.png', center=(8, 8), width=16, depth=16)
        self.assertEqual(metadata['status'], 'ok')

    def test_corrupt_zlib_chunk_is_reported_not_fatal(self):
        write_region(self.world, 0, 0, {(0, 0): flat_chunk(0, 0), (1, 0): flat_chunk(1, 0)})
        metadata = self.render(self.root / 'ok.png', center=(8, 8), width=32, depth=16)
        self.assertEqual(metadata['status'], 'partial')  # region r.-1.0 missing
        region = self.region()
        raw = bytearray(region.read_bytes())
        start = region_chunk_start(region, 1)
        raw[start + 5] ^= 0xFF  # invalid zlib header in chunk (1,0)
        region.write_bytes(bytes(raw))
        metadata = self.render(self.root / 'zlib.png', center=(8, 8), width=32, depth=16)
        self.assertEqual(metadata['status'], 'partial')
        self.assertEqual(metadata['chunks']['read'], 1)
        self.assertEqual(metadata['chunks']['corrupt'], 1)
        self.assertTrue(any('zlib' in message for message in metadata['errors']))

    def test_non_path_arguments_and_oversized_areas_are_rejected(self):
        write_region(self.world, 0, 0, {(0, 0): flat_chunk(0, 0)})
        with self.assertRaises(WorldRenderError):
            render_world(None, self.root / 'map.png')
        with self.assertRaises(WorldRenderError):
            render_world(self.world, None)
        with self.assertRaises(WorldRenderError):
            self.render(self.root / 'map.png', center=(8, 8), width=1, depth=1024)
        with self.assertRaises(WorldRenderError):
            self.render(self.root / 'map.png', center=(8, 8), width=512, depth=512, size=(64, 64))

    def _png_bytes(self, width, height, rows, color_type=6):
        """Minimal PNG encoder for decoder fixtures (filter 0 rows)."""
        def chunk(kind, payload):
            return (struct.pack('>I', len(payload)) + kind + payload
                    + struct.pack('>I', zlib.crc32(kind + payload) & 0xFFFFFFFF))
        channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
        raw = b''.join(b'\x00' + bytes(row) for row in rows)
        header = struct.pack('>IIBBBBB', width, height, 8, color_type, 0, 0, 0)
        return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header)
                + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))

    def test_mesh_png_decoder_and_material_sampling(self):
        rows = [bytes([10, 20, 30, 255, 40, 50, 60, 128]) for _ in range(2)]
        width, height, pixels = decode_png(self._png_bytes(2, 2, [rows[0], rows[1]]))
        self.assertEqual((width, height, len(pixels)), (2, 2, 16))
        self.assertEqual(tuple(pixels[0:4]), (10, 20, 30, 255))
        self.assertEqual(tuple(pixels[4:8]), (40, 50, 60, 128))
        # Palette PNG decoding (type 3).
        def chunk(kind, payload):
            return struct.pack('>I', len(payload)) + kind + payload + \
                struct.pack('>I', zlib.crc32(kind + payload) & 0xFFFFFFFF)
        palette = bytes([255, 0, 0, 0, 255, 0])
        header = struct.pack('>IIBBBBB', 2, 1, 8, 3, 0, 0, 0)
        raw = b'\x00' + bytes([0, 1])
        palette_png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header)
                       + chunk(b'PLTE', palette) + chunk(b'IDAT', zlib.compress(raw))
                       + chunk(b'IEND', b''))
        width, height, pixels = decode_png(palette_png)
        self.assertEqual((width, height), (2, 1))
        self.assertEqual(tuple(pixels[0:4]), (255, 0, 0, 255))
        self.assertEqual(tuple(pixels[4:8]), (0, 255, 0, 255))
        with self.assertRaises(ValueError):
            decode_png(b'not a png')
        # Real Pixel Perfection texture -> 16x16 material with nearest sampling.
        material = block_material('minecraft:grass_block')
        top = material['top']
        self.assertEqual((top[0], top[1]), (16, 16))
        corner = sampled_material_pixel(top, 0.01, 0.01)
        self.assertEqual(len(corner), 4)
        self.assertTrue(any(material[face][0] * material[face][1] > 1 for face in material))
        fallback = block_material('minecraft:modded_widget_block')
        self.assertEqual((fallback['top'][0], fallback['top'][1]), (1, 1))
        self.assertEqual(tuple(fallback['top'][2]), tuple(color_for_block('minecraft:modded_widget_block')) + (255,))
        water = block_material('minecraft:water')['top']
        self.assertEqual(water[2][3], 186)  # translucent fallback alpha

    def test_mesh_face_culling_and_extent_bounds(self):
        self.assertTrue(_visible_face('minecraft:stone', 'minecraft:air'))
        self.assertTrue(_visible_face('minecraft:grass_block', None))
        self.assertFalse(_visible_face('minecraft:stone', 'minecraft:stone'))
        self.assertFalse(_visible_face('minecraft:oak_leaves', 'minecraft:oak_leaves'))
        self.assertTrue(_visible_face('minecraft:stone', 'minecraft:water'))
        self.assertFalse(_visible_face('minecraft:water', 'minecraft:water'))
        self.assertFalse(_visible_face('minecraft:air', 'minecraft:air'))
        self.assertEqual(_mesh_extent(16, 20), (16, 20))
        self.assertEqual(_mesh_extent(512, 512), (MESH_MAX_COLUMNS, 160))
        self.assertEqual(_mesh_extent(200, 10), (MESH_MAX_COLUMNS, 10))

    def test_mesh_render_is_deterministic_and_bounded(self):
        write_region(self.world, 0, 0, {(0, 0): flat_chunk(0, 0)})
        output = self.root / 'mesh.png'
        before = self.region().read_bytes()
        metadata = self.render(output, style='mesh', center=(8, 8), width=16, depth=16)
        data = output.read_bytes()
        self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II', data[16:24]), (1024, 640))
        self.assertEqual(metadata['style'], 'mesh')
        self.assertEqual(metadata['status'], 'ok')
        self.assertGreater(metadata['faces']['emitted'], 0)
        self.assertGreater(metadata['faces']['drawn'], 0)
        self.assertLessEqual(metadata['faces']['emitted'], metadata['faces']['max'])
        self.assertEqual(metadata['mesh_window']['width'], 16)
        self.assertEqual(metadata['mesh_window']['depth'], 16)
        self.assertGreater(metadata['mesh_window']['voxels'], 0)
        self.assertIn('minecraft:grass_block', metadata['materials']['textured_blocks'])
        self.assertEqual(metadata['camera']['mode'], 'perspective_aerial')
        self.assertTrue(metadata['read_only'])
        self.assertTrue(metadata['world_files_unchanged'])
        self.assertIn('textured 3D mesh', metadata['caption'])
        self.assertEqual(metadata['png_sha256'], hashlib.sha256(data).hexdigest())
        again = self.render(output, style='mesh', center=(8, 8), width=16, depth=16)
        self.assertEqual(again['png_sha256'], metadata['png_sha256'])
        self.assertEqual(self.region().read_bytes(), before)
        # Oversized extents are clamped to the bounded mesh window, not expanded.
        wide = self.render(self.root / 'wide.png', style='mesh', center=(8, 8), width=200, depth=10)
        self.assertEqual(wide['mesh_window']['width'], MESH_MAX_COLUMNS)
        self.assertEqual(wide['mesh_window']['depth'], 10)

    def test_z_buffer_occlusion_is_order_independent(self):
        width = height = 8
        pixels = bytearray(width * height * 3)
        zbuffer = [float('inf')] * (width * height)
        red = (1, 1, bytearray((255, 0, 0, 255)))
        blue = (1, 1, bytearray((0, 0, 255, 255)))
        points = [(-2.0, -2.0), (10.0, -2.0), (10.0, 10.0)]
        uvs = [(0.0, 0.0), (0.0, 0.0), (0.0, 0.0)]
        # Far surface first, then a nearer surface: the nearer colour must win.
        _raster_triangle(pixels, zbuffer, width, height, points, [10.0, 10.0, 10.0],
                         uvs, red, 1.0)
        _raster_triangle(pixels, zbuffer, width, height, points, [5.0, 5.0, 5.0],
                         uvs, blue, 1.0)
        centre = (4 * width + 4) * 3
        self.assertEqual(tuple(pixels[centre:centre + 3]), (0, 0, 255))
        self.assertAlmostEqual(zbuffer[4 * width + 4], 5.0, places=4)
        # Redrawing the far surface cannot overwrite the winner.
        _raster_triangle(pixels, zbuffer, width, height, points, [10.0, 10.0, 10.0],
                         uvs, red, 1.0)
        self.assertEqual(tuple(pixels[centre:centre + 3]), (0, 0, 255))
        self.assertAlmostEqual(zbuffer[4 * width + 4], 5.0, places=4)

    def test_full_world_mesh_covers_every_saved_chunk(self):
        write_region(self.world, 0, 0, {(0, 0): flat_chunk(0, 0)})
        write_region(self.world, -1, 0, {(-1, 0): flat_chunk(-1, 0)})
        metadata = self.render(self.root / 'full.png', style='mesh', width=0, depth=0,
                               full_world=True)
        self.assertEqual(metadata['mode'], 'full_world')
        self.assertTrue(metadata['full_world'])
        self.assertEqual(metadata['saved_world']['saved_chunks'], 2)
        self.assertEqual(metadata['saved_world']['covered_chunks'], 2)
        self.assertGreater(metadata['saved_world']['coverage_ratio'], 0.99)
        self.assertEqual(metadata['saved_world']['region_files'], ['r.-1.0.mca', 'r.0.0.mca'])
        self.assertEqual(metadata['saved_world']['world_bounds']['x0'], -16)
        self.assertEqual(metadata['saved_world']['world_bounds']['x1'], 16)
        self.assertEqual(metadata['chunks']['requested'], 2)
        self.assertEqual(metadata['chunks']['read'], 2)
        self.assertEqual(metadata['lod']['cell_blocks'], 1)
        self.assertLessEqual(metadata['lod']['cells_rendered'], MAX_FULL_WORLD_CELLS)
        self.assertGreater(metadata['faces']['emitted'], 0)
        self.assertTrue(metadata['depth_buffer']['enabled'])
        self.assertGreater(metadata['depth_buffer']['tests'], 0)
        self.assertEqual(metadata['camera']['sort'], 'per_pixel_z_buffer')
        self.assertIn('complete saved overworld', metadata['caption'])
        data = (self.root / 'full.png').read_bytes()
        self.assertEqual(struct.unpack('>II', data[16:24]), (1024, 640))
        again = self.render(self.root / 'full.png', style='mesh', width=0, depth=0)
        self.assertEqual(again['png_sha256'], metadata['png_sha256'])
        self.assertEqual(metadata['png_hash'] if False else metadata['png_sha256'],
                         hashlib.sha256(data).hexdigest())

    def test_mesh_diorama_uses_depth_buffer(self):
        write_region(self.world, 0, 0, {(0, 0): flat_chunk(0, 0)})
        metadata = self.render(self.root / 'di.png', style='mesh', center=(8, 8), width=16, depth=16)
        self.assertEqual(metadata['mode'], 'diorama')
        self.assertTrue(metadata['depth_buffer']['enabled'])
        self.assertEqual(metadata['depth_buffer']['type'], 'per_pixel_z')
        self.assertGreater(metadata['depth_buffer']['tests'], 0)
        self.assertGreater(metadata['depth_buffer']['writes'], 0)
        self.assertEqual(metadata['camera']['sort'], 'per_pixel_z_buffer')

    def test_world_fingerprint_tracks_region_changes(self):
        write_region(self.world, 0, 0, {(0, 0): flat_chunk(0, 0)})
        first = world_fingerprint(self.world)
        self.assertIn('sha256', first)
        self.assertIn('r.0.0.mca', first['regions'])
        self.assertEqual(first['level_dat']['size'], (self.world / 'level.dat').stat().st_size)
        region = self.region()
        region.write_bytes(region.read_bytes() + b'\x00' * 4096)
        self.assertNotEqual(world_fingerprint(self.world)['sha256'], first['sha256'])
        with self.assertRaises(WorldRenderError):
            world_fingerprint(self.world / 'absent')


if __name__ == '__main__':
    unittest.main()
