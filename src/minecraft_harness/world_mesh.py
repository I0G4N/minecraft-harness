"""Read-only textured 3D mesh renderer for a saved Minecraft Java world.

This module is the ``style="mesh"`` backend of :mod:`minecraft_harness.world_render`.
It reuses the bounded Anvil reader from that module, builds a small voxel volume
around the spawn/base (at most ``MESH_MAX_COLUMNS`` x ``MESH_MAX_COLUMNS``
columns and ``MESH_MAX_Y`` blocks tall), emits only block faces whose neighbour
is air/translucent, and rasterises them with a perspective aerial camera using
Pixel Perfection 16x16 textures from ``static/ui-assets`` (pure-stdlib PNG
decoder + nearest-neighbour sampling) with deterministic colour fallbacks.

The world is only read; the PNG output path is the only file written.  All
limits are explicit (volume, faces, texture sizes, region/chunk sizes inherited
from :mod:`world_render`).  The camera, lighting, sort order and rasteriser are
fully deterministic.
"""
from __future__ import annotations

import array
import hashlib
import math
import os
import re
import struct
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

from .environment import read_level
from .world_render import (
    DEFAULT_DEPTH,
    MAX_CHUNKS,
    DEFAULT_SIZE,
    DEFAULT_WIDTH,
    SUPPORTED_STYLES,
    WorldRenderError,
    _chunk_plan,
    _encode_png,
    _read_level_identity,
    _Region,
    _validate_arguments,
    _world_path,
    _write_output,
    AIR,
    chunk_surface,
    color_for_block,
    decode_block_states,
    parse_chunk_nbt,
    world_fingerprint,
)

SCHEMA_VERSION = 1
RENDERER = "minecraft-harness-world-render"
STYLE = "mesh"
TEXTURE_DIR = Path(__file__).resolve().parent / "static" / "ui-assets"
MAX_TEXTURE_BYTES = 4 * 1024 ** 2
MAX_TEXTURE_DIMENSION = 1024
MESH_MAX_COLUMNS = 160
MESH_MAX_DEPTH = 160
MESH_MAX_Y = 64
MAX_MESH_FACES = 200000
MAX_MESH_VOXELS = 160 * 160 * 64
MAX_FULL_WORLD_CELLS = 20000
FULL_WORLD_LOD_STEPS = (1, 2, 4, 8, 16)
MAX_FULL_WORLD_FACES = 120000
INTERNAL_SIZE = (512, 320)  # rendered then nearest-upscaled to the requested size
CAMERA_AZIMUTH_DEG = 45.0
CAMERA_ELEVATION_DEG = 35.0
CAMERA_FOV_DEG = 38.0
SKY_TOP = (24, 38, 62)
SKY_HORIZON = (120, 150, 184)
# Under this fixed aerial camera only the +Y, +X and +Z faces can face the viewer.
FRONT_FACES = ((0, 1, 0), (1, 0, 0), (0, 0, 1))
FACE_SHADE = {
    (0, 1, 0): 1.0, (0, -1, 0): 0.42,
    (1, 0, 0): 0.74, (-1, 0, 0): 0.58,
    (0, 0, 1): 0.66, (0, 0, -1): 0.52,
}
# Blocks that do not occlude their neighbours (they are blended over the terrain).
TRANSLUCENT = frozenset({
    "minecraft:water", "minecraft:lava", "minecraft:glass", "minecraft:glass_pane",
    "minecraft:ice", "minecraft:blue_ice", "minecraft:packed_ice", "minecraft:slime_block",
    "minecraft:honey_block", "minecraft:nether_portal", "minecraft:end_portal",
})
TRANSLUCENT_ALPHA = {"minecraft:water": 186, "minecraft:ice": 210, "minecraft:glass": 120,
                     "minecraft:glass_pane": 120, "minecraft:nether_portal": 200,
                     "minecraft:end_portal": 220}
_TEXTURE_CANDIDATES = {
    "minecraft:grass_block": {"top": ("grass_block_top",), "side": ("grass_block_side",), "bottom": ("dirt",)},
    "minecraft:dirt": {"side": ("dirt",), "top": ("dirt",), "bottom": ("dirt",)},
    "minecraft:coarse_dirt": {"side": ("coarse_dirt",), "top": ("coarse_dirt",), "bottom": ("coarse_dirt",)},
    "minecraft:stone": {"side": ("stone",), "top": ("stone",), "bottom": ("stone",)},
    "minecraft:smooth_stone": {"side": ("stone",), "top": ("stone",), "bottom": ("stone",)},
    "minecraft:cobblestone": {"side": ("cobblestone",), "top": ("cobblestone",), "bottom": ("cobblestone",)},
    "minecraft:mossy_cobblestone": {"side": ("mossy_cobblestone",), "top": ("mossy_cobblestone",), "bottom": ("mossy_cobblestone",)},
    "minecraft:oak_planks": {"side": ("oak_planks",), "top": ("oak_planks",), "bottom": ("oak_planks",)},
    "minecraft:oak_log": {"side": ("oak_log",), "top": ("oak_log_top",), "bottom": ("oak_log_top",)},
    "minecraft:bookshelf": {"side": ("bookshelf",), "top": ("oak_planks",), "bottom": ("oak_planks",)},
    "minecraft:crafting_table": {"side": ("crafting_table_front",), "top": ("crafting_table_top",),
                                 "bottom": ("oak_planks",)},
    "minecraft:obsidian": {"side": ("obsidian",), "top": ("obsidian",), "bottom": ("obsidian",)},
    "minecraft:netherrack": {"side": ("netherrack",), "top": ("netherrack",), "bottom": ("netherrack",)},
    "minecraft:diamond_block": {"side": ("diamond_block",), "top": ("diamond_block",), "bottom": ("diamond_block",)},
    "minecraft:gold_block": {"side": ("gold_block",), "top": ("gold_block",), "bottom": ("gold_block",)},
    "minecraft:iron_block": {"side": ("iron_block",), "top": ("iron_block",), "bottom": ("iron_block",)},
    "minecraft:lapis_block": {"side": ("lapis_block",), "top": ("lapis_block",), "bottom": ("lapis_block",)},
    "minecraft:emerald_block": {"side": ("emerald_block",), "top": ("emerald_block",), "bottom": ("emerald_block",)},
    "minecraft:redstone_block": {"side": ("redstone_block",), "top": ("redstone_block",), "bottom": ("redstone_block",)},
    "minecraft:soul_lantern": {"side": ("soul_lantern",), "top": ("soul_lantern",), "bottom": ("soul_lantern",)},
    "minecraft:lever": {"side": ("lever",), "top": ("lever",), "bottom": ("lever",)},
}
_DECODED_TEXTURES: dict = {}
_MATERIALS: dict = {}
_SOLID_TEXTURES: dict = {}


# ---------------------------------------------------------------------------
# Pure-stdlib PNG decoding (8/16-bit RGB/RGBA/palette/grey, non-interlaced).

def decode_png(raw: bytes):
    """Decode a bounded PNG into ``(width, height, bytearray RGBA)``."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_TEXTURE_BYTES:
        raise ValueError("PNG payload is not a bounded byte string")
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    position = 8
    width = height = depth = color_type = interlace = None
    palette = None
    transparency = None
    compressed = bytearray()
    while position + 8 <= len(raw):
        length = struct.unpack(">I", raw[position:position + 4])[0]
        kind = raw[position + 4:position + 8]
        start = position + 8
        end = start + length
        if end + 4 > len(raw):
            raise ValueError("truncated PNG chunk")
        data = raw[start:end]
        position = end + 4
        if kind == b"IHDR":
            if length != 13:
                raise ValueError("invalid PNG header")
            width, height, depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", data)
        elif kind == b"PLTE":
            if length % 3 or not 0 < length <= 768:
                raise ValueError("invalid PNG palette")
            palette = data
        elif kind == b"tRNS":
            transparency = data
        elif kind == b"IDAT":
            compressed += data
        elif kind == b"IEND":
            break
    if width is None or not (0 < width <= MAX_TEXTURE_DIMENSION and 0 < height <= MAX_TEXTURE_DIMENSION):
        raise ValueError("PNG dimensions are missing or out of bounds")
    if interlace != 0:
        raise ValueError("interlaced PNGs are not supported")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None or depth not in (1, 2, 4, 8, 16):
        raise ValueError("unsupported PNG colour format")
    if color_type == 3 and palette is None:
        raise ValueError("palette PNG without PLTE chunk")
    bytes_per_pixel = max(1, (channels * depth + 7) // 8)
    stride = (width * channels * depth + 7) // 8
    try:
        filtered = zlib.decompress(bytes(compressed))
    except zlib.error as exc:
        raise ValueError(f"invalid PNG image data: {exc}") from exc
    if len(filtered) != height * (stride + 1):
        raise ValueError("PNG image data length mismatch")
    rows = []
    previous = bytearray(stride)
    position = 0
    for _ in range(height):
        filter_type = filtered[position]
        position += 1
        row = bytearray(filtered[position:position + stride])
        position += stride
        if filter_type == 1:
            for index in range(bytes_per_pixel, stride):
                row[index] = (row[index] + row[index - bytes_per_pixel]) & 0xFF
        elif filter_type == 2:
            for index in range(stride):
                row[index] = (row[index] + previous[index]) & 0xFF
        elif filter_type == 3:
            for index in range(stride):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                row[index] = (row[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif filter_type == 4:
            for index in range(stride):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                upper = previous[index]
                upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                estimate = left + upper - upper_left
                pa, pb, pc = abs(estimate - left), abs(estimate - upper), abs(estimate - upper_left)
                predictor = left if pa <= pb and pa <= pc else (upper if pb <= pc else upper_left)
                row[index] = (row[index] + predictor) & 0xFF
        elif filter_type != 0:
            raise ValueError(f"unsupported PNG filter {filter_type}")
        rows.append(row)
        previous = row
    rgba = bytearray(width * height * 4)

    def sample(row, index):
        if depth == 8:
            return row[index]
        if depth == 16:
            return row[index * 2]
        bit = index * depth
        return (row[bit >> 3] >> (8 - depth - (bit & 7))) & ((1 << depth) - 1)

    def scale(value):
        return value * 255 // ((1 << depth) - 1) if depth < 8 else value

    for y in range(height):
        row = rows[y]
        for x in range(width):
            target = (y * width + x) * 4
            if color_type == 6:
                rgba[target:target + 4] = row[x * 4:x * 4 + 4]
            elif color_type == 2:
                rgba[target:target + 3] = row[x * 3:x * 3 + 3]
                rgba[target + 3] = 255
            elif color_type == 4:
                grey = row[x * 2]
                rgba[target] = rgba[target + 1] = rgba[target + 2] = grey
                rgba[target + 3] = row[x * 2 + 1]
            elif color_type == 0:
                grey = scale(sample(row, x))
                rgba[target] = rgba[target + 1] = rgba[target + 2] = grey
                alpha = 255
                if transparency is not None and depth == 8 and len(transparency) >= 2:
                    if grey == struct.unpack(">H", transparency[:2])[0] & 0xFF:
                        alpha = 0
                rgba[target + 3] = alpha
            else:  # palette
                index = sample(row, x) if depth < 8 else row[x]
                if index * 3 + 2 >= len(palette):
                    raise ValueError("PNG palette index out of range")
                rgba[target:target + 3] = palette[index * 3:index * 3 + 3]
                alpha = 255
                if transparency is not None and index < len(transparency):
                    alpha = transparency[index]
                rgba[target + 3] = alpha
    return width, height, rgba


def _load_texture(name: str):
    """Load ``<name>.png`` from the ui-assets directory, or None if unavailable."""
    cached = _DECODED_TEXTURES.get(name)
    if cached is not None or name in _DECODED_TEXTURES:
        return cached
    path = TEXTURE_DIR / f"{name}.png"
    result = None
    try:
        if path.is_file() and not path.is_symlink():
            raw = path.read_bytes()
            if len(raw) <= MAX_TEXTURE_BYTES:
                result = decode_png(raw)
    except (OSError, ValueError):
        result = None
    _DECODED_TEXTURES[name] = result
    return result


def _solid_texture(color, alpha=255):
    key = (tuple(color), int(alpha))
    texture = _SOLID_TEXTURES.get(key)
    if texture is None:
        texture = (1, 1, bytearray((color[0], color[1], color[2], alpha)))
        _SOLID_TEXTURES[key] = texture
    return texture


def block_material(name: str):
    """Return ``{"top","side","bottom"}`` textures for a block id.

    Each texture is ``(width, height, RGBA bytearray)``.  Blocks without an
    available texture get a deterministic solid-colour fallback derived from
    :func:`world_render.color_for_block`.
    """
    cached = _MATERIALS.get(name)
    if cached is not None:
        return cached
    fallback_alpha = TRANSLUCENT_ALPHA.get(name, 255)
    fallback = _solid_texture(color_for_block(name), fallback_alpha if name in TRANSLUCENT else 255)
    candidates = _TEXTURE_CANDIDATES.get(name, {})
    material = {}
    for face in ("top", "side", "bottom"):
        texture = fallback
        for candidate in candidates.get(face, ()):
            loaded = _load_texture(candidate)
            if loaded is not None:
                texture = loaded
                break
        material[face] = texture
    _MATERIALS[name] = material
    return material


def sampled_material_pixel(texture, u: float, v: float, wrap: bool = False):
    """Nearest-neighbour texture sample; UVs are clamped or tiled modulo 1."""
    width, height, pixels = texture
    if wrap:
        u -= math.floor(u)
        v -= math.floor(v)
    x = min(width - 1, max(0, int(u * width)))
    y = min(height - 1, max(0, int(v * height)))
    offset = (y * width + x) * 4
    return pixels[offset], pixels[offset + 1], pixels[offset + 2], pixels[offset + 3]


# ---------------------------------------------------------------------------
# Voxel extraction and visible-face generation.

def _is_air(name) -> bool:
    return name is None or name in AIR


def _visible_face(block: str, neighbour) -> bool:
    """True when ``block`` shows a face toward ``neighbour`` (None = outside)."""
    if _is_air(neighbour):
        return not _is_air(block)
    if neighbour in TRANSLUCENT:
        return neighbour != block
    return False


def _mesh_extent(width: int, depth: int):
    """Clamp the requested extents to the bounded mesh window."""
    return min(width, MESH_MAX_COLUMNS), min(depth, MESH_MAX_DEPTH)


def _decoded_section_blocks(chunk: dict):
    """Yield ``(section_y, palette, indices)`` for a parsed chunk."""
    for section in chunk["sections"]:
        states = section["block_states"]
        palette = tuple(states["palette"])
        if not palette:
            continue
        indices = decode_block_states(states["data"], len(palette))
        yield section["y"], palette, indices


def _build_volume(chunks: dict, *, x0: int, z0: int, width: int, depth: int,
                  y_lo: int, y_hi: int):
    """Fill a bounded voxel volume; return (volume, materials, material_index).

    ``volume`` is a flat byte array of material ids (0 = air) ordered as
    ``(y * width + x) * depth + z``.
    """
    material_index: dict = {}
    materials: list = []
    volume = bytearray(width * (y_hi - y_lo + 1) * depth)

    def material_id(name):
        value = material_index.get(name)
        if value is None:
            value = len(materials) + 1
            material_index[name] = value
            materials.append(name)
        return value

    for (chunk_x, chunk_z), chunk in chunks.items():
        base_x = chunk_x * 16
        base_z = chunk_z * 16
        for section in chunk["sections"]:
            section_base_y = section["y"] * 16
            if section_base_y + 15 < y_lo or section_base_y > y_hi:
                continue
            states = section["block_states"]
            palette = tuple(states["palette"])
            if not palette:
                continue
            indices = decode_block_states(states["data"], len(palette))
            for i in range(4096):
                world_y = section_base_y + (i >> 8)
                if world_y < y_lo or world_y > y_hi:
                    continue
                world_x = base_x + (i & 15)
                world_z = base_z + ((i >> 4) & 15)
                if not (x0 <= world_x < x0 + width and z0 <= world_z < z0 + depth):
                    continue
                name = palette[0] if indices is None else palette[indices[i]]
                if name in AIR:
                    continue
                offset = ((world_y - y_lo) * width + (world_x - x0)) * depth + (world_z - z0)
                volume[offset] = material_id(name)
    return volume, materials, material_index


def _extract_faces(volume, materials, *, x0: int, z0: int, width: int, depth: int,
                   y_lo: int, y_hi: int):
    """Emit camera-facing quads whose neighbour is air/translucent/outside."""
    height = y_hi - y_lo + 1
    faces = []
    for y in range(height):
        for x in range(width):
            row_base = (y * width + x) * depth
            for z in range(depth):
                material_value = volume[row_base + z]
                if material_value == 0:
                    continue
                block = materials[material_value - 1]
                for direction in FRONT_FACES:
                    dx, dy, dz = direction
                    nx, ny, nz = x + dx, y + dy, z + dz
                    if 0 <= nx < width and 0 <= ny < height and 0 <= nz < depth:
                        neighbour_id = volume[(ny * width + nx) * depth + nz]
                        neighbour = None if neighbour_id == 0 else materials[neighbour_id - 1]
                    else:
                        neighbour = None
                    if not _visible_face(block, neighbour):
                        continue
                    faces.append((block, direction, x0 + x, y_lo + y, z0 + z))
                    if len(faces) > MAX_MESH_FACES:
                        raise WorldRenderError(
                            f"visible mesh faces exceed the {MAX_MESH_FACES}-face render budget; "
                            "reduce the mesh window")
    return faces


# ---------------------------------------------------------------------------
# Camera, projection and textured rasteriser.

class _Camera:
    __slots__ = ("position", "right", "up", "forward", "focal", "center_x", "center_y")

    def __init__(self, target, distance: float, width: int, height: int):
        azimuth = math.radians(CAMERA_AZIMUTH_DEG)
        elevation = math.radians(CAMERA_ELEVATION_DEG)
        direction = (math.cos(elevation) * math.sin(azimuth),
                     math.sin(elevation),
                     math.cos(elevation) * math.cos(azimuth))
        self.position = tuple(target[i] + direction[i] * distance for i in range(3))
        forward = tuple(target[i] - self.position[i] for i in range(3))
        length = math.sqrt(sum(value * value for value in forward)) or 1.0
        self.forward = tuple(value / length for value in forward)
        world_up = (0.0, 1.0, 0.0)
        right = (self.forward[1] * world_up[2] - self.forward[2] * world_up[1],
                 self.forward[2] * world_up[0] - self.forward[0] * world_up[2],
                 self.forward[0] * world_up[1] - self.forward[1] * world_up[0])
        right_length = math.sqrt(sum(value * value for value in right)) or 1.0
        self.right = tuple(value / right_length for value in right)
        self.up = (self.right[1] * self.forward[2] - self.right[2] * self.forward[1],
                   self.right[2] * self.forward[0] - self.right[0] * self.forward[2],
                   self.right[0] * self.forward[1] - self.right[1] * self.forward[0])
        self.focal = (height / 2.0) / math.tan(math.radians(CAMERA_FOV_DEG) / 2.0)
        self.center_x = width / 2.0
        self.center_y = height / 2.0

    def project(self, point):
        relative = (point[0] - self.position[0], point[1] - self.position[1], point[2] - self.position[2])
        depth = relative[0] * self.forward[0] + relative[1] * self.forward[1] + relative[2] * self.forward[2]
        if depth <= 0.05:
            return None
        x_camera = relative[0] * self.right[0] + relative[1] * self.right[1] + relative[2] * self.right[2]
        y_camera = relative[0] * self.up[0] + relative[1] * self.up[1] + relative[2] * self.up[2]
        return (self.center_x + self.focal * x_camera / depth,
                self.center_y - self.focal * y_camera / depth, depth)


_QUAD_CORNERS = {
    (0, 1, 0): ((0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)),
    (1, 0, 0): ((1, 1, 0), (1, 1, 1), (1, 0, 1), (1, 0, 0)),
    (0, 0, 1): ((0, 1, 1), (1, 1, 1), (1, 0, 1), (0, 0, 1)),
}
_QUAD_UVS = {
    (0, 1, 0): ((0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)),
    (1, 0, 0): ((1.0, 0.0), (0.0, 0.0), (0.0, 1.0), (1.0, 1.0)),
    (0, 0, 1): ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
}


def _blend(pixels, offset: int, red: int, green: int, blue: int, alpha: int) -> None:
    if alpha >= 255:
        pixels[offset] = red
        pixels[offset + 1] = green
        pixels[offset + 2] = blue
        return
    if alpha <= 0:
        return
    inverse = 255 - alpha
    pixels[offset] = (red * alpha + pixels[offset] * inverse + 127) // 255
    pixels[offset + 1] = (green * alpha + pixels[offset + 1] * inverse + 127) // 255
    pixels[offset + 2] = (blue * alpha + pixels[offset + 2] * inverse + 127) // 255


def _raster_triangle(pixels, zbuffer, width: int, height: int, points, depths, uvs,
                     texture, shade: float, wrap: bool = False) -> tuple[int, int]:
    """Rasterise one affine-textured triangle with a real per-pixel z-buffer.

    Depth is interpolated in screen space as ``1/z`` (perspective correct for
    planar faces) and tested against ``zbuffer``; only nearer pixels are
    blended/written.  Returns ``(depth_tests, writes)``.
    """
    (x0, y0), (x1, y1), (x2, y2) = points
    (u0, v0), (u1, v1), (u2, v2) = uvs
    (d0, d1, d2) = depths
    i0, i1, i2 = 1.0 / d0, 1.0 / d1, 1.0 / d2
    minimum_y = max(0, int(math.floor(min(y0, y1, y2))))
    maximum_y = min(height - 1, int(math.ceil(max(y0, y1, y2))))
    tests = writes = 0
    for pixel_y in range(minimum_y, maximum_y + 1):
        scan_y = pixel_y + 0.5
        intersections = []
        for (ax, ay, au, av, ai), (bx, by, bu, bv, bi) in (
                ((x0, y0, u0, v0, i0), (x1, y1, u1, v1, i1)),
                ((x1, y1, u1, v1, i1), (x2, y2, u2, v2, i2)),
                ((x2, y2, u2, v2, i2), (x0, y0, u0, v0, i0))):
            if ay == by:
                continue
            if (ay <= scan_y < by) or (by <= scan_y < ay):
                t = (scan_y - ay) / (by - ay)
                intersections.append((ax + t * (bx - ax), au + t * (bu - au),
                                      av + t * (bv - av), ai + t * (bi - ai)))
        if len(intersections) < 2:
            continue
        intersections.sort()
        left, right = intersections[0], intersections[-1]
        if right[0] - left[0] <= 1e-9:
            continue
        start_x = max(0, int(math.ceil(left[0] - 0.5)))
        end_x = min(width - 1, int(math.floor(right[0] - 0.5 + 0.35)))
        inverse_span = 1.0 / (right[0] - left[0])
        for pixel_x in range(start_x, end_x + 1):
            t = (pixel_x + 0.5 - left[0]) * inverse_span
            inverse_depth = left[3] + t * (right[3] - left[3])
            if inverse_depth <= 0:
                continue
            depth = 1.0 / inverse_depth
            target = pixel_y * width + pixel_x
            tests += 1
            if depth >= zbuffer[target]:
                continue
            u = left[1] + t * (right[1] - left[1])
            v = left[2] + t * (right[2] - left[2])
            red, green, blue, alpha = sampled_material_pixel(texture, u, v, wrap)
            if alpha <= 0:
                continue
            writes += 1
            offset = target * 3
            _blend(pixels, offset,
                   min(255, int(red * shade + 0.5)),
                   min(255, int(green * shade + 0.5)),
                   min(255, int(blue * shade + 0.5)), alpha)
            zbuffer[target] = depth
    return tests, writes


def _project_quad(camera, block_position, direction):
    projected = []
    base_x, base_y, base_z = block_position
    for corner in _QUAD_CORNERS[direction]:
        result = camera.project((base_x + corner[0], base_y + corner[1], base_z + corner[2]))
        if result is None:
            return None
        projected.append(result)
    return projected


def _draw_quad(pixels, zbuffer, width: int, height: int, projected, direction,
               texture, shade: float, tile: tuple[float, float] = (1.0, 1.0)) -> tuple[int, int]:
    """Draw a textured world-space quad through two z-buffered triangles."""
    points = [(point[0], point[1]) for point in projected]
    depths = [point[2] for point in projected]
    uvs = [(u * tile[0], v * tile[1]) for u, v in _QUAD_UVS[direction]]
    wrap = tile[0] != 1.0 or tile[1] != 1.0
    tests_a, writes_a = _raster_triangle(
        pixels, zbuffer, width, height, points[:3], depths[:3], uvs[:3], texture, shade, wrap)
    tests_b, writes_b = _raster_triangle(
        pixels, zbuffer, width, height, (points[0], points[2], points[3]),
        (depths[0], depths[2], depths[3]), (uvs[0], uvs[2], uvs[3]), texture, shade, wrap)
    return tests_a + tests_b, writes_a + writes_b


def _sky_pixels(internal_w: int, internal_h: int) -> bytearray:
    pixels = bytearray(internal_w * internal_h * 3)
    for row in range(internal_h):
        fraction = row / max(1, internal_h - 1)
        color = tuple(int(SKY_TOP[i] + (SKY_HORIZON[i] - SKY_TOP[i]) * fraction + 0.5) for i in range(3))
        offset = row * internal_w * 3
        pixels[offset:offset + internal_w * 3] = bytes(color) * internal_w
    return pixels


def _fit_camera(bounds, internal_w: int, internal_h: int, content_corners=None) -> _Camera:
    """Deterministic perspective aerial camera fitted to a volume/content corners."""
    x0, z0, x1, z1, y_lo, y_hi = bounds
    target = ((x0 + x1) / 2.0, (y_lo + y_hi) / 2.0, (z0 + z1) / 2.0)
    distance = math.sqrt((x1 - x0) ** 2 + (z1 - z0) ** 2 + (y_hi - y_lo) ** 2) * 1.15
    corners = ((x0, y_lo, z0), (x1, y_lo, z0), (x0, y_lo, z1), (x1, y_lo, z1),
               (x0, y_hi, z0), (x1, y_hi, z0), (x0, y_hi, z1), (x1, y_hi, z1))
    camera = _Camera(target, distance, internal_w, internal_h)
    for _ in range(4):
        projected = [value for value in (camera.project(corner) for corner in corners) if value is not None]
        if not projected:
            break
        span_x = max(value[0] for value in projected) - min(value[0] for value in projected)
        span_y = max(value[1] for value in projected) - min(value[1] for value in projected)
        ratio = max((span_x * 1.08) / internal_w, (span_y * 1.08) / internal_h)
        if 0.86 <= ratio <= 1.02:
            break
        distance *= max(0.55, min(2.2, ratio))
        camera = _Camera(target, distance, internal_w, internal_h)
    if content_corners:
        for _ in range(3):
            projected = [value for value in (camera.project(corner) for corner in content_corners)
                         if value is not None]
            if len(projected) < 2:
                break
            span_x = max(value[0] for value in projected) - min(value[0] for value in projected)
            span_y = max(value[1] for value in projected) - min(value[1] for value in projected)
            ratio = max((span_x * 1.04) / internal_w, (span_y * 1.04) / internal_h)
            if ratio <= 0.0:
                break
            distance *= max(0.6, min(1.6, ratio / 0.98))
            camera = _Camera(target, distance, internal_w, internal_h)
    return camera


def _upscale(pixels, internal_w: int, internal_h: int, pixel_w: int, pixel_h: int) -> bytearray:
    output = bytearray(pixel_w * pixel_h * 3)
    for y in range(pixel_h):
        source_y = min(internal_h - 1, (y * internal_h) // pixel_h)
        for x in range(pixel_w):
            source_x = min(internal_w - 1, (x * internal_w) // pixel_w)
            source = (source_y * internal_w + source_x) * 3
            target_offset = (y * pixel_w + x) * 3
            output[target_offset:target_offset + 3] = pixels[source:source + 3]
    return output


def _render_mesh(faces, *, x0: int, z0: int, width: int, depth: int,
                 y_lo: int, y_hi: int, pixel_w: int, pixel_h: int):
    """Render the bounded diorama mesh with a per-pixel z-buffer."""
    internal_w, internal_h = INTERNAL_SIZE
    camera = _fit_camera((x0, z0, x0 + width, z0 + depth, y_lo, y_hi), internal_w, internal_h)
    pixels = _sky_pixels(internal_w, internal_h)
    zbuffer = array.array("f", [float("inf")]) * (internal_w * internal_h)
    drawable = []
    for block, direction, block_x, block_y, block_z in faces:
        projected = _project_quad(camera, (block_x, block_y, block_z), direction)
        if projected is None:
            continue
        depth_sum = sum(point[2] for point in projected)
        drawable.append((depth_sum, block_x, block_y, block_z, direction, projected, block))
    drawable.sort(key=lambda item: (-item[0], item[1], item[2], item[3], item[4]))
    material_cache: dict = {}
    texture_blocks = set()
    depth_tests = writes = 0
    for _, block_x, block_y, block_z, direction, projected, block in drawable:
        material = material_cache.get(block)
        if material is None:
            material = block_material(block)
            material_cache[block] = material
            if material["top"][0] * material["top"][1] > 1:
                texture_blocks.add(block)
        face_name = "top" if direction[1] == 1 else "side"
        tests, written = _draw_quad(pixels, zbuffer, internal_w, internal_h, projected,
                                    direction, material[face_name], FACE_SHADE[direction])
        depth_tests += tests
        writes += written
    zbuffer_stats = {"tests": depth_tests, "writes": writes}
    return _upscale(pixels, internal_w, internal_h, pixel_w, pixel_h), drawable, texture_blocks, zbuffer_stats, camera


# ---------------------------------------------------------------------------
# Full saved-world LOD heightfield mesh.

_REGION_NAME = re.compile(r"r\.(-?\d+)\.(-?\d+)\.mca")


def _saved_chunks(world: Path):
    """Enumerate every chunk present in the saved overworld region files."""
    region_dir = world / "region"
    saved = []
    region_files = []
    for path in sorted(region_dir.iterdir()):
        match = _REGION_NAME.fullmatch(path.name)
        if not match or not path.is_file():
            continue
        if path.is_symlink():
            raise WorldRenderError(f"region file must not be a symlink: {path}")
        region_x, region_z = int(match.group(1)), int(match.group(2))
        region = _Region(path)
        try:
            header = region.header
            for index in range(1024):
                location = struct.unpack_from(">I", header, index * 4)[0]
                if location == 0:
                    continue
                saved.append((region_x * 32 + index % 32, region_z * 32 + index // 32))
        finally:
            region.close()
        region_files.append(path.name)
    if not saved:
        raise WorldRenderError("saved world contains no region chunks")
    if len(saved) > MAX_CHUNKS:
        raise WorldRenderError(
            f"saved world has {len(saved)} chunks, exceeding the {MAX_CHUNKS}-chunk render budget")
    return sorted(saved), region_files


def _read_saved_chunks(world: Path, saved):
    """Read the given chunk coordinates, grouped by region file."""
    chunks: dict = {}
    stats = {"requested": len(saved), "read": 0, "missing": 0, "corrupt": 0}
    errors: list[str] = []
    sections_read = 0
    data_versions: dict = {}
    grouped: dict = {}
    for chunk_x, chunk_z in saved:
        grouped.setdefault((chunk_x // 32, chunk_z // 32), []).append((chunk_x, chunk_z))

    def note(message: str) -> None:
        if len(errors) < 20:
            errors.append(message)

    for (region_x, region_z), coords in sorted(grouped.items()):
        path = world / "region" / f"r.{region_x}.{region_z}.mca"
        if path.is_symlink():
            raise WorldRenderError(f"region file must not be a symlink: {path}")
        if not path.is_file():
            stats["missing"] += len(coords)
            note(f"region file missing: {path.name} ({len(coords)} chunks)")
            continue
        region = _Region(path)
        try:
            for chunk_x, chunk_z in coords:
                try:
                    payload = region.chunk_payload(chunk_x, chunk_z)
                except ValueError as exc:
                    stats["corrupt"] += 1
                    note(f"chunk {chunk_x},{chunk_z}: {exc}")
                    continue
                if payload is None:
                    stats["missing"] += 1
                    note(f"chunk {chunk_x},{chunk_z}: not generated in {path.name}")
                    continue
                try:
                    chunk = parse_chunk_nbt(payload)
                except ValueError as exc:
                    stats["corrupt"] += 1
                    note(f"chunk {chunk_x},{chunk_z}: {exc}")
                    continue
                if chunk.get("x") != chunk_x or chunk.get("z") != chunk_z:
                    stats["corrupt"] += 1
                    note(f"chunk {chunk_x},{chunk_z}: coordinate mismatch in chunk NBT")
                    continue
                stats["read"] += 1
                sections_read += len(chunk["sections"])
                version = chunk.get("data_version")
                if isinstance(version, int):
                    data_versions[str(version)] = data_versions.get(str(version), 0) + 1
                chunks[(chunk_x, chunk_z)] = chunk
        finally:
            region.close()
    return chunks, stats, errors, sections_read, data_versions


def _full_world_cells(chunks: dict, *, cell_blocks: int):
    """Reduce decoded column surfaces into max-height LOD cells covering all chunks."""
    cells: dict = {}
    covered = set()
    columns = 0
    for (chunk_x, chunk_z), chunk in chunks.items():
        try:
            tops, heights, _ = chunk_surface(chunk)
        except ValueError:
            continue
        base_x, base_z = chunk_x * 16, chunk_z * 16
        chunk_covered = False
        for column in range(256):
            height = heights[column]
            top = tops[column]
            if height is None or top is None:
                continue
            columns += 1
            chunk_covered = True
            cell = (((base_x + (column & 15)) // cell_blocks),
                    ((base_z + (column >> 4)) // cell_blocks))
            current = cells.get(cell)
            if current is None or height > current[0]:
                cells[cell] = (height, top)
        if chunk_covered:
            covered.add((chunk_x, chunk_z))
    return cells, covered, columns


def _heightfield_faces(cells: dict, *, cell_blocks: int):
    """Top quads plus camera-facing side quads for the LOD heightfield."""
    faces = []
    for (cell_x, cell_z), (height, top) in sorted(cells.items()):
        x = cell_x * cell_blocks
        z = cell_z * cell_blocks
        faces.append(("top", x, height, z, cell_blocks, 0, top))
        neighbour = cells.get((cell_x + 1, cell_z))
        if neighbour is not None and height > neighbour[0]:
            faces.append(("+x", x + cell_blocks, neighbour[0], z, cell_blocks,
                          height - neighbour[0], top))
        neighbour = cells.get((cell_x, cell_z + 1))
        if neighbour is not None and height > neighbour[0]:
            faces.append(("+z", x, neighbour[0], z + cell_blocks, cell_blocks,
                          height - neighbour[0], top))
        if len(faces) > MAX_FULL_WORLD_FACES:
            raise WorldRenderError(
                f"full-world mesh exceeds the {MAX_FULL_WORLD_FACES}-face render budget")
    return faces


def _heightfield_corners(kind: str, x: int, y: int, z: int, span: int, dh: int):
    if kind == "top":
        return ((x, y, z), (x, y, z + span), (x + span, y, z + span), (x + span, y, z))
    if kind == "+x":
        return ((x, y, z), (x, y, z + span), (x, y + dh, z + span), (x, y + dh, z))
    return ((x, y, z), (x + span, y, z), (x + span, y + dh, z), (x, y + dh, z))


def _heightfield_uvs(kind: str, span: int, dh: int):
    width = span
    height = dh if kind != "top" else span
    return ((0.0, 0.0), (float(width), 0.0), (float(width), float(height)), (0.0, float(height)))


def _draw_textured_quad(pixels, zbuffer, width: int, height: int, projected, uvs,
                        texture, shade: float) -> tuple[int, int]:
    points = [(point[0], point[1]) for point in projected]
    depths = [point[2] for point in projected]
    tests_a, writes_a = _raster_triangle(pixels, zbuffer, width, height, points[:3], depths[:3],
                                         uvs[:3], texture, shade, True)
    tests_b, writes_b = _raster_triangle(
        pixels, zbuffer, width, height, (points[0], points[2], points[3]),
        (depths[0], depths[2], depths[3]), (uvs[0], uvs[2], uvs[3]), texture, shade, True)
    return tests_a + tests_b, writes_a + writes_b


def _render_full_world(cells: dict, *, cell_blocks: int, pixel_w: int, pixel_h: int):
    """Render every saved chunk as one shared, z-buffered LOD heightfield mesh."""
    xs = [cell[0] for cell in cells]
    zs = [cell[1] for cell in cells]
    heights = [value[0] for value in cells.values()]
    x0, x1 = min(xs) * cell_blocks, (max(xs) + 1) * cell_blocks
    z0, z1 = min(zs) * cell_blocks, (max(zs) + 1) * cell_blocks
    y_lo, y_hi = min(heights), max(heights) + 1
    internal_w, internal_h = INTERNAL_SIZE
    descriptions = _heightfield_faces(cells, cell_blocks=cell_blocks)
    content_corners = []
    for kind, x, y, z, span, dh, _top in descriptions:
        content_corners.extend(_heightfield_corners(kind, x, y, z, span, dh))
    camera = _fit_camera((x0, z0, x1, z1, y_lo, y_hi), internal_w, internal_h,
                         content_corners=content_corners)
    pixels = _sky_pixels(internal_w, internal_h)
    zbuffer = array.array("f", [float("inf")]) * (internal_w * internal_h)
    drawable = []
    for kind, x, y, z, span, dh, top in descriptions:
        corners = _heightfield_corners(kind, x, y, z, span, dh)
        projected = []
        for corner in corners:
            value = camera.project(corner)
            if value is None:
                break
            projected.append(value)
        if len(projected) != 4:
            continue
        drawable.append((sum(point[2] for point in projected), corners, projected,
                         kind, span, dh, top))
    drawable.sort(key=lambda item: (-item[0], item[1]))
    material_cache: dict = {}
    texture_blocks = set()
    depth_tests = writes = 0
    for _, corners, projected, kind, span, dh, top in drawable:
        material = material_cache.get(top)
        if material is None:
            material = block_material(top)
            material_cache[top] = material
            if any(material[face][0] * material[face][1] > 1 for face in material):
                texture_blocks.add(top)
        texture = material["top"] if kind == "top" else material["side"]
        shade = FACE_SHADE[(0, 1, 0)] if kind == "top" else FACE_SHADE[(1, 0, 0)]
        uvs = _heightfield_uvs(kind, span, dh)
        tests, written = _draw_textured_quad(pixels, zbuffer, internal_w, internal_h,
                                             projected, uvs, texture, shade)
        depth_tests += tests
        writes += written
    zbuffer_stats = {"tests": depth_tests, "writes": writes}
    return (_upscale(pixels, internal_w, internal_h, pixel_w, pixel_h), drawable,
            texture_blocks, zbuffer_stats, (x0, z0, x1, z1, y_lo, y_hi), camera)


def _read_chunks_for_mesh(world: Path, plan: dict, region_dir: Path):
    """Read every planned chunk, returning (chunks, stats, errors)."""
    chunks: dict = {}
    stats = {"requested": sum(len(value) for value in plan.values()),
             "read": 0, "missing": 0, "corrupt": 0}
    errors: list[str] = []
    sections_read = 0
    data_versions: dict = {}

    def note(message: str) -> None:
        if len(errors) < 20:
            errors.append(message)

    for region_key, chunk_coords in plan.items():
        region_x, region_z = region_key
        path = region_dir / f"r.{region_x}.{region_z}.mca"
        if path.is_symlink():
            raise WorldRenderError(f"region file must not be a symlink: {path}")
        if not path.is_file():
            stats["missing"] += len(chunk_coords)
            note(f"region file missing: {path.name} ({len(chunk_coords)} chunks)")
            continue
        region = _Region(path)
        try:
            for chunk_x, chunk_z in chunk_coords:
                try:
                    payload = region.chunk_payload(chunk_x, chunk_z)
                except ValueError as exc:
                    stats["corrupt"] += 1
                    note(f"chunk {chunk_x},{chunk_z}: {exc}")
                    continue
                if payload is None:
                    stats["missing"] += 1
                    note(f"chunk {chunk_x},{chunk_z}: not generated in {path.name}")
                    continue
                try:
                    chunk = parse_chunk_nbt(payload)
                except ValueError as exc:
                    stats["corrupt"] += 1
                    note(f"chunk {chunk_x},{chunk_z}: {exc}")
                    continue
                if chunk.get("x") != chunk_x or chunk.get("z") != chunk_z:
                    stats["corrupt"] += 1
                    note(f"chunk {chunk_x},{chunk_z}: coordinate mismatch in chunk NBT")
                    continue
                stats["read"] += 1
                sections_read += len(chunk["sections"])
                version = chunk.get("data_version")
                if isinstance(version, int):
                    data_versions[str(version)] = data_versions.get(str(version), 0) + 1
                chunks[(chunk_x, chunk_z)] = chunk
        finally:
            region.close()
    return chunks, stats, errors, sections_read, data_versions


def _base_metadata(*, style, status, world, identity, spawn, center_x, center_z, bounds,
                   output, pixel_w, pixel_h, png, fingerprint_before, fingerprint_after,
                   errors, caption, method, limitations, generated_at, started, extra):
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "renderer": RENDERER,
        "status": status,
        "style": style,
        "read_only": True,
        "world_dir": str(world),
        "level_name": identity["level_name"],
        "minecraft_version": identity["minecraft_version"],
        "spawn": spawn,
        "center": {"x": center_x, "z": center_z},
        "bounds": bounds,
        "output": str(output),
        "size": [pixel_w, pixel_h],
        "png_bytes": len(png),
        "png_sha256": hashlib.sha256(png).hexdigest(),
        "errors": errors,
        "limitations": limitations,
        "caption": caption,
        "method": method,
        "input_fingerprint": {"sha256": fingerprint_before["sha256"],
                              "regions": fingerprint_before["regions"],
                              "level_dat": fingerprint_before["level_dat"]},
        "world_files_unchanged": fingerprint_before["sha256"] == fingerprint_after["sha256"],
        "generated_at": generated_at,
        "elapsed_s": round(time.monotonic() - started, 3),
    }
    metadata.update(extra)
    return metadata


def _render_diorama_metadata(*, world, identity, output_path, center_x, center_z, width, depth,
                             pixel_w, pixel_h, fingerprint_before, started, generated_at):
    mesh_w, mesh_d = _mesh_extent(width, depth)
    x0 = center_x - mesh_w // 2
    z0 = center_z - mesh_d // 2
    plan = _chunk_plan((x0, z0, x0 + mesh_w, z0 + mesh_d))
    region_dir = world / "region"
    chunks, stats, errors, sections_read, data_versions = _read_chunks_for_mesh(world, plan, region_dir)
    if stats["read"] == 0:
        detail = "; ".join(errors[:3]) or "no chunks were found in the requested area"
        raise WorldRenderError(f"no readable chunks for the requested area ({detail})")
    heights = []
    for chunk in chunks.values():
        try:
            _, chunk_heights, _ = chunk_surface(chunk)
        except ValueError as exc:
            stats["corrupt"] += 1
            errors.append(f"surface extraction failed: {exc}")
            continue
        heights.extend(value for value in chunk_heights if value is not None)
    if not heights:
        raise WorldRenderError("chunk data did not yield any resolvable block columns")
    y_lo = max(-64, min(heights) - 2)
    y_hi = min(319, max(heights) + 3)
    if y_hi - y_lo + 1 > MESH_MAX_Y:
        y_hi = y_lo + MESH_MAX_Y - 1
    volume, materials, _ = _build_volume(
        chunks, x0=x0, z0=z0, width=mesh_w, depth=mesh_d, y_lo=y_lo, y_hi=y_hi)
    if not materials:
        raise WorldRenderError("mesh window contains no blocks in the sampled y range")
    faces = _extract_faces(volume, materials, x0=x0, z0=z0, width=mesh_w, depth=mesh_d,
                           y_lo=y_lo, y_hi=y_hi)
    if not faces:
        raise WorldRenderError("mesh produced no visible faces")
    pixels, drawable, texture_blocks, zbuffer_stats, camera = _render_mesh(
        faces, x0=x0, z0=z0, width=mesh_w, depth=mesh_d, y_lo=y_lo, y_hi=y_hi,
        pixel_w=pixel_w, pixel_h=pixel_h)
    png = _encode_png(pixels, pixel_w, pixel_h)
    output = _write_output(output_path, png)
    fingerprint_after = world_fingerprint(world)
    status = "ok" if not stats["missing"] and not stats["corrupt"] else "partial"
    texture_names = sorted(name for name in materials if any(
        block_material(name)[face][0] * block_material(name)[face][1] > 1
        for face in ("top", "side", "bottom")))
    caption = (
        f"Read-only textured 3D mesh render of the saved Minecraft world '{identity['level_name']}' "
        f"around X {x0}..{x0 + mesh_w - 1}, Z {z0}..{z0 + mesh_d - 1}, Y {y_lo}..{y_hi} "
        f"({mesh_w}x{mesh_d} columns, {len(faces)} visible block faces) at a "
        f"{CAMERA_ELEVATION_DEG:.0f} deg aerial perspective view; per-pixel z-buffer; "
        f"Pixel Perfection textures where available, deterministic colour fallbacks otherwise.")
    method = (
        "Bounded Anvil voxel extraction from 1.20.4 section block-state palettes; emits camera-facing "
        "block quads whose neighbour is air/translucent/outside the volume; perspective aerial camera, "
        "real per-pixel z-buffer, directional face shading, nearest-neighbour 16x16 texture sampling "
        "through a pure-stdlib PNG decoder, deterministic stdlib PNG encoder.")
    limitations = [
        "Only the overworld region/ directory is read; DIM-1/DIM1 saves are ignored.",
        "Requires 1.16+ compact block-state packing (DataVersion >= 2529); older chunk layouts are reported as corrupt.",
        "LZ4-compressed and external (.mcc) chunks are rejected as unsupported.",
        f"Mesh window is bounded to {MESH_MAX_COLUMNS}x{MESH_MAX_DEPTH} columns and {MESH_MAX_Y} vertical blocks "
        "around the sampled surface; the volume is a diorama cut-out, not the full world.",
        "Per-pixel z-buffer resolves occlusion; translucent faces are still alpha-blended in far-to-near order.",
        "Only 16x16 ui-assets textures are used; blocks without a texture get deterministic flat colours, and no block "
        "entities, biomes, lighting, fluid levels or transparency beyond simple alpha blending are modelled.",
        "Missing/corrupt chunks render as gaps and are counted; an actively saving server can produce them.",
        "No text is drawn into the image; the dashboard supplies the caption from this metadata.",
    ]
    return _base_metadata(
        style=STYLE, status=status, world=world, identity=identity, spawn=identity["spawn"],
        center_x=center_x, center_z=center_z,
        bounds={"x0": x0, "x1": x0 + mesh_w, "z0": z0, "z1": z0 + mesh_d},
        output=output, pixel_w=pixel_w, pixel_h=pixel_h, png=png,
        fingerprint_before=fingerprint_before, fingerprint_after=fingerprint_after,
        errors=errors, caption=caption, method=method, limitations=limitations,
        generated_at=generated_at, started=started,
        extra={
            "mode": "diorama",
            "requested_extent": {"width": width, "depth": depth},
            "mesh_window": {"width": mesh_w, "depth": mesh_d, "y_min": y_lo, "y_max": y_hi,
                            "voxels": mesh_w * (y_hi - y_lo + 1) * mesh_d,
                            "max_columns": MESH_MAX_COLUMNS, "max_y": MESH_MAX_Y},
            "faces": {"emitted": len(faces), "drawn": len(drawable), "max": MAX_MESH_FACES},
            "materials": {"distinct_blocks": len(materials), "textured_blocks": texture_names,
                          "textured_count": len(texture_blocks),
                          "texture_dir": str(TEXTURE_DIR)},
            "camera": {"azimuth_deg": CAMERA_AZIMUTH_DEG, "elevation_deg": CAMERA_ELEVATION_DEG,
                       "fov_deg": CAMERA_FOV_DEG, "mode": "perspective_aerial",
                       "sort": "per_pixel_z_buffer"},
            "depth_buffer": {"enabled": True, "type": "per_pixel_z", "width": INTERNAL_SIZE[0],
                             "height": INTERNAL_SIZE[1], "tests": zbuffer_stats["tests"],
                             "writes": zbuffer_stats["writes"]},
            "chunks": stats,
            "sections_read": sections_read,
            "data_versions": dict(sorted(data_versions.items())),
            "internal_size": list(INTERNAL_SIZE),
            "projection": {
                "camera": {
                    "position": list(camera.position), "right": list(camera.right),
                    "up": list(camera.up), "forward": list(camera.forward),
                    "focal": camera.focal, "center_x": camera.center_x, "center_y": camera.center_y,
                },
                "internal_size": list(INTERNAL_SIZE),
                "output_size": [pixel_w, pixel_h],
                "world_bounds": {"x0": x0, "x1": x0 + mesh_w, "z0": z0, "z1": z0 + mesh_d,
                                 "y0": y_lo, "y1": y_hi},
                "eye_height": 1.62,
            },
        })


def _render_full_world_metadata(*, world, identity, output_path, pixel_w, pixel_h,
                                fingerprint_before, started, generated_at):
    saved, region_files = _saved_chunks(world)
    chunks, stats, errors, sections_read, data_versions = _read_saved_chunks(world, saved)
    if stats["read"] == 0:
        raise WorldRenderError("saved world yielded no readable chunks")
    cell_blocks = FULL_WORLD_LOD_STEPS[-1]
    for step in FULL_WORLD_LOD_STEPS:
        if len(saved) * (16 // step) ** 2 <= MAX_FULL_WORLD_CELLS:
            cell_blocks = step
            break
    cells, covered, columns = _full_world_cells(chunks, cell_blocks=cell_blocks)
    if not cells:
        raise WorldRenderError("full-world mesh has no resolvable terrain cells")
    faces = _heightfield_faces(cells, cell_blocks=cell_blocks)
    if not faces:
        raise WorldRenderError("full-world mesh produced no visible faces")
    (pixels, drawable, texture_blocks, zbuffer_stats,
     (x0, z0, x1, z1, y_lo, y_hi), _camera) = _render_full_world(
        cells, cell_blocks=cell_blocks, pixel_w=pixel_w, pixel_h=pixel_h)
    png = _encode_png(pixels, pixel_w, pixel_h)
    output = _write_output(output_path, png)
    fingerprint_after = world_fingerprint(world)
    status = "ok" if not stats["missing"] and not stats["corrupt"] else "partial"
    coverage_ratio = round(len(covered) / len(saved), 4) if saved else 0.0
    caption = (
        f"Read-only textured 3D LOD mesh of the complete saved overworld '{identity['level_name']}': "
        f"{len(saved)} saved chunks in {len(region_files)} region files, full extent X {x0}..{x1 - 1}, "
        f"Z {z0}..{z1 - 1}, Y {y_lo}..{y_hi}; {cell_blocks}x{cell_blocks}-block LOD cells "
        f"({len(cells)} cells, {len(faces)} visible faces, chunk coverage {coverage_ratio:.1%}) at a "
        f"{CAMERA_ELEVATION_DEG:.0f} deg aerial perspective view; per-pixel z-buffer; "
        f"Pixel Perfection textures where available, deterministic colour fallbacks otherwise.")
    method = (
        "Enumerates every chunk in the saved overworld region files (location tables), bounded by "
        f"{MAX_CHUNKS} chunks; decodes 1.20.4 section block-state palettes, reduces column surfaces to "
        f"max-height {cell_blocks}x{cell_blocks}-block LOD cells covering every saved chunk; emits top and "
        "camera-facing side quads; perspective aerial camera with a real per-pixel z-buffer, directional "
        "shading, nearest-neighbour 16x16 texture sampling (pure-stdlib PNG decoder) and a deterministic "
        "stdlib PNG encoder.")
    limitations = [
        "Only the overworld region/ directory is read; DIM-1/DIM1 saves are ignored.",
        "Requires 1.16+ compact block-state packing (DataVersion >= 2529); older chunk layouts are reported as corrupt.",
        "LZ4-compressed and external (.mcc) chunks are rejected as unsupported.",
        f"Full-world mesh is LOD-reduced to max-height {cell_blocks}x{cell_blocks}-block cells (max "
        f"{MAX_FULL_WORLD_CELLS} cells) so every saved chunk fits the render budget; it is a coarse terrain "
        "heightfield, not block-accurate 1:1 voxels.",
        "Cells with no saved chunk data are holes (background sky) rather than terrain; chunks with no decodable "
        "surface blocks are counted in saved_world.chunks_without_surface.",
        "Per-pixel z-buffer resolves occlusion; translucent faces are still alpha-blended in far-to-near order.",
        "Only 16x16 ui-assets textures are used; blocks without a texture get deterministic flat colours; no block "
        "entities, biomes, lighting or fluid levels are modelled.",
        "Missing/corrupt chunks render as gaps and are counted; an actively saving server can produce them.",
        "No text is drawn into the image; the dashboard supplies the caption from this metadata.",
    ]
    return _base_metadata(
        style=STYLE, status=status, world=world, identity=identity, spawn=identity["spawn"],
        center_x=(x0 + x1) // 2, center_z=(z0 + z1) // 2,
        bounds={"x0": x0, "x1": x1, "z0": z0, "z1": z1},
        output=output, pixel_w=pixel_w, pixel_h=pixel_h, png=png,
        fingerprint_before=fingerprint_before, fingerprint_after=fingerprint_after,
        errors=errors, caption=caption, method=method, limitations=limitations,
        generated_at=generated_at, started=started,
        extra={
            "mode": "full_world",
            "full_world": True,
            "saved_world": {"saved_chunks": len(saved), "region_files": region_files,
                            "covered_chunks": len(covered),
                            "chunks_with_surface": len(covered),
                            "chunks_without_surface": len(saved) - len(covered),
                            "coverage_ratio": coverage_ratio,
                            "world_bounds": {"x0": x0, "x1": x1, "z0": z0, "z1": z1,
                                             "y_min": y_lo, "y_max": y_hi}},
            "lod": {"cell_blocks": cell_blocks, "cells_rendered": len(cells),
                    "columns_sampled": columns, "max_cells": MAX_FULL_WORLD_CELLS,
                    "steps": list(FULL_WORLD_LOD_STEPS)},
            "faces": {"emitted": len(faces), "drawn": len(drawable), "max": MAX_FULL_WORLD_FACES},
            "materials": {"distinct_blocks": len({top for _, top in cells.values()}),
                          "textured_blocks": sorted(texture_blocks),
                          "textured_count": len(texture_blocks),
                          "texture_dir": str(TEXTURE_DIR)},
            "camera": {"azimuth_deg": CAMERA_AZIMUTH_DEG, "elevation_deg": CAMERA_ELEVATION_DEG,
                       "fov_deg": CAMERA_FOV_DEG, "mode": "perspective_aerial",
                       "sort": "per_pixel_z_buffer"},
            "depth_buffer": {"enabled": True, "type": "per_pixel_z", "width": INTERNAL_SIZE[0],
                             "height": INTERNAL_SIZE[1], "tests": zbuffer_stats["tests"],
                             "writes": zbuffer_stats["writes"]},
            "chunks": stats,
            "sections_read": sections_read,
            "data_versions": dict(sorted(data_versions.items())),
            "internal_size": list(INTERNAL_SIZE),
        })


def render_mesh_world(world_dir, output_path, *, center=None, width=DEFAULT_WIDTH,
                      depth=DEFAULT_DEPTH, size=DEFAULT_SIZE, full_world=False) -> dict:
    """Render the textured 3D mesh PNG (bounded diorama or full saved world)."""
    started = time.monotonic()
    generated_at = datetime.now(timezone.utc).isoformat()
    full = bool(full_world) or width == 0 or depth == 0
    if full:
        pixel_w, pixel_h = _validate_arguments(1, 1, "top", size, None)
    else:
        pixel_w, pixel_h = _validate_arguments(width, depth, "top", size, center)
    world = _world_path(world_dir)
    identity = _read_level_identity(world)
    spawn = identity["spawn"]
    if center is not None:
        center_x, center_z = int(round(center[0])), int(round(center[1]))
    else:
        center_x, center_z = spawn["x"], spawn["z"]
    fingerprint_before = world_fingerprint(world)
    if full:
        return _render_full_world_metadata(
            world=world, identity=identity, output_path=output_path,
            pixel_w=pixel_w, pixel_h=pixel_h,
            fingerprint_before=fingerprint_before, started=started, generated_at=generated_at)
    return _render_diorama_metadata(
        world=world, identity=identity, output_path=output_path,
        center_x=center_x, center_z=center_z, width=width, depth=depth,
        pixel_w=pixel_w, pixel_h=pixel_h,
        fingerprint_before=fingerprint_before, started=started, generated_at=generated_at)
