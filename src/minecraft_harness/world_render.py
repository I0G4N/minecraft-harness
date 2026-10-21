"""Read-only deterministic block-map renderer for a saved Minecraft Java world.

The renderer reads only the vanilla Anvil region files (``region/r.X.Z.mca``) and
``level.dat`` of an overworld save.  It never creates, deletes, moves or writes a
world file: every read is a bounded ``O_RDONLY`` open.  The only files created
are the requested output PNG and, when they do not exist yet, its parent
directories (the output path itself is symlink-checked).  No external packages
are required.

What is rendered
----------------
For every sampled block column the highest non-air block is decoded from the
1.20.4 chunk section ``block_states`` palettes and compact packed longs.  A
top-down RGB map is drawn with the block's deterministic map colour, shaded by
the relative terrain height (a north-west hill-shade plus side bevels where the
column is higher than its south/east neighbour) and framed with coordinate
ticks, a height bar, a 16-block scale bar and a spawn marker.  The image carries
no text: the dashboard supplies the caption from the returned metadata.

Limitations (also reported in ``metadata["limitations"]``)
----------------------------------------------------------
* Only the overworld ``region/`` directory is read; ``DIM-1``/``DIM1`` saves
  are ignored.
* Only chunk ``DataVersion`` >= 2529 (1.16+) compact block-state packing is
  understood; older layouts are reported as chunk errors.
* LZ4-compressed and external (``.mcc``) chunks are rejected as unsupported.
* No block entities, biomes, lighting, textures, transparency or true 3D
  meshing are used; the highest block *is* the visible column.
* An actively saving server can yield missing or corrupt chunks; they render as
  dark unknown cells and are counted instead of failing the whole image.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import os
import stat
import struct
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

from .environment import read_level


SCHEMA_VERSION = 1
RENDERER = "minecraft-harness-world-render"
DEFAULT_SIZE = (1024, 640)
DEFAULT_WIDTH = 64
DEFAULT_DEPTH = 64
MAX_CHUNKS = 1024
MAX_COLUMNS = 65536
MAX_DIMENSION = 4096
MAX_CHUNK_NBT = 16 * 1024 ** 2
MAX_REGION_BYTES = 256 * 1024 ** 2
MAX_REGION_HEADER = 8192
MIN_REGION_BYTES = MAX_REGION_HEADER
MAX_ARCHIVE_SECTORS = 255
SUPPORTED_STYLES = ("top", "iso", "mesh")
MAX_REPORTED_ERRORS = 20
MAX_REPORTED_BLOCKS = 16
MAX_REPORTED_UNKNOWN = 16
HEIGHT_TICK_BLOCKS = 8
SCALE_BAR_BLOCKS = 16

MISSING_COLOR = (42, 48, 58)
AIR = frozenset({
    "minecraft:air", "minecraft:cave_air", "minecraft:void_air", "minecraft:structure_void",
    "minecraft:light", "minecraft:barrier",
})
# Pure decoration does not define the surface: the ground below is shown instead.
DECORATION = frozenset({
    "minecraft:grass", "minecraft:short_grass", "minecraft:tall_grass", "minecraft:fern",
    "minecraft:large_fern", "minecraft:dead_bush", "minecraft:vine", "minecraft:ladder",
    "minecraft:torch", "minecraft:wall_torch", "minecraft:soul_torch", "minecraft:redstone_torch",
    "minecraft:lantern", "minecraft:soul_lantern", "minecraft:chain", "minecraft:lever",
    "minecraft:tripwire", "minecraft:tripwire_hook", "minecraft:rail", "minecraft:powered_rail",
    "minecraft:detector_rail", "minecraft:activator_rail", "minecraft:snow", "minecraft:sugar_cane",
    "minecraft:wheat", "minecraft:carrots", "minecraft:potatoes", "minecraft:beetroots",
    "minecraft:sweet_berry_bush", "minecraft:kelp", "minecraft:kelp_plant", "minecraft:seagrass",
    "minecraft:tall_seagrass", "minecraft:sea_pickle", "minecraft:lily_pad",
    "minecraft:dandelion", "minecraft:poppy", "minecraft:blue_orchid", "minecraft:allium",
    "minecraft:azure_bluet", "minecraft:red_tulip", "minecraft:orange_tulip",
    "minecraft:white_tulip", "minecraft:pink_tulip", "minecraft:oxeye_daisy",
    "minecraft:cornflower", "minecraft:lily_of_the_valley", "minecraft:wither_rose",
    "minecraft:oak_sapling", "minecraft:birch_sapling", "minecraft:spruce_sapling",
    "minecraft:jungle_sapling", "minecraft:acacia_sapling", "minecraft:dark_oak_sapling",
    "minecraft:cherry_sapling", "minecraft:mangrove_propagule", "minecraft:bamboo_sapling",
    "minecraft:brown_mushroom", "minecraft:red_mushroom", "minecraft:crimson_roots",
    "minecraft:warped_roots", "minecraft:nether_sprouts", "minecraft:bamboo", "minecraft:cactus",
    "minecraft:fire", "minecraft:soul_fire", "minecraft:frogspawn",
})
# Canopies keep the leaf colour but do not lift the terrain height used for relief.
LEAFY = frozenset({
    "minecraft:oak_leaves", "minecraft:birch_leaves", "minecraft:spruce_leaves",
    "minecraft:jungle_leaves", "minecraft:acacia_leaves", "minecraft:dark_oak_leaves",
    "minecraft:mangrove_leaves", "minecraft:cherry_leaves", "minecraft:azalea_leaves",
    "minecraft:flowering_azalea_leaves",
})


class WorldRenderError(ValueError):
    """A render could not be produced from the supplied world/output paths."""


# ---------------------------------------------------------------------------
# Deterministic block colour map.

def _rgb(value: int) -> tuple[int, int, int]:
    return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF


BLOCK_COLORS = {
    # terrain
    "minecraft:grass_block": _rgb(0x6FA84C),
    "minecraft:podzol": _rgb(0x6B5030),
    "minecraft:mycelium": _rgb(0x7C6C74),
    "minecraft:dirt": _rgb(0x8A6844),
    "minecraft:coarse_dirt": _rgb(0x7A5A3A),
    "minecraft:rooted_dirt": _rgb(0x8E6E4E),
    "minecraft:farmland": _rgb(0x6E4B2A),
    "minecraft:dirt_path": _rgb(0x9C7F4E),
    "minecraft:sand": _rgb(0xE1D6A4),
    "minecraft:red_sand": _rgb(0xBE6B2B),
    "minecraft:gravel": _rgb(0x8C8680),
    "minecraft:clay": _rgb(0xA3A7B2),
    "minecraft:stone": _rgb(0x8B8B8B),
    "minecraft:cobblestone": _rgb(0x6F6F6F),
    "minecraft:mossy_cobblestone": _rgb(0x62805A),
    "minecraft:smooth_stone": _rgb(0x9C9C9C),
    "minecraft:stone_bricks": _rgb(0x7C7C7C),
    "minecraft:mossy_stone_bricks": _rgb(0x6B7F5F),
    "minecraft:cracked_stone_bricks": _rgb(0x737373),
    "minecraft:andesite": _rgb(0x8A8A87),
    "minecraft:diorite": _rgb(0xC4C4C0),
    "minecraft:granite": _rgb(0x9A6B5B),
    "minecraft:tuff": _rgb(0x6D6F64),
    "minecraft:calcite": _rgb(0xD8D8D0),
    "minecraft:dripstone_block": _rgb(0x866B5E),
    "minecraft:deepslate": _rgb(0x54545A),
    "minecraft:cobbled_deepslate": _rgb(0x4A4A50),
    "minecraft:polished_deepslate": _rgb(0x4E4E55),
    "minecraft:deepslate_bricks": _rgb(0x45454B),
    "minecraft:deepslate_tiles": _rgb(0x3F3F45),
    "minecraft:basalt": _rgb(0x515156),
    "minecraft:smooth_basalt": _rgb(0x48484D),
    "minecraft:blackstone": _rgb(0x2D2329),
    "minecraft:obsidian": _rgb(0x1A1128),
    "minecraft:crying_obsidian": _rgb(0x261644),
    "minecraft:bedrock": _rgb(0x333333),
    "minecraft:netherrack": _rgb(0x722828),
    "minecraft:soul_sand": _rgb(0x54413A),
    "minecraft:soul_soil": _rgb(0x4F3C33),
    "minecraft:end_stone": _rgb(0xDFE3A8),
    "minecraft:end_stone_bricks": _rgb(0xCFD394),
    "minecraft:purpur_block": _rgb(0xA97BA9),
    "minecraft:magma_block": _rgb(0x8E451F),
    "minecraft:glowstone": _rgb(0xDDBB66),
    "minecraft:sea_lantern": _rgb(0xC6D9CF),
    "minecraft:terracotta": _rgb(0x985F43),
    "minecraft:bricks": _rgb(0x96574A),
    "minecraft:prismarine": _rgb(0x6F9F93),
    "minecraft:prismarine_bricks": _rgb(0x6AA396),
    "minecraft:dark_prismarine": _rgb(0x4C7A6A),
    "minecraft:ice": _rgb(0x9FC6EC),
    "minecraft:packed_ice": _rgb(0x8CB6E6),
    "minecraft:blue_ice": _rgb(0x74A7E8),
    "minecraft:snow_block": _rgb(0xF4F8F8),
    "minecraft:powder_snow": _rgb(0xEAF3F7),
    "minecraft:moss_block": _rgb(0x5E7D3A),
    "minecraft:mud": _rgb(0x3C3733),
    "minecraft:mud_bricks": _rgb(0x8A7A5E),
    "minecraft:packed_mud": _rgb(0x9C8255),
    "minecraft:amethyst_block": _rgb(0x8566B0),
    "minecraft:deepslate_coal_ore": _rgb(0x4B4B50),
    # fluids
    "minecraft:water": _rgb(0x3D6BC4),
    "minecraft:lava": _rgb(0xE05A1E),
    # wood: logs are darker, planks lighter, leaves green.
    "minecraft:oak_log": _rgb(0x6B5334),
    "minecraft:oak_planks": _rgb(0xB08A55),
    "minecraft:oak_leaves": _rgb(0x4E7B2E),
    "minecraft:birch_log": _rgb(0xD7CFB8),
    "minecraft:birch_planks": _rgb(0xC7B287),
    "minecraft:birch_leaves": _rgb(0x6E9240),
    "minecraft:spruce_log": _rgb(0x4A3826),
    "minecraft:spruce_planks": _rgb(0x7D5A38),
    "minecraft:spruce_leaves": _rgb(0x35592A),
    "minecraft:jungle_log": _rgb(0x77633F),
    "minecraft:jungle_planks": _rgb(0xAD8558),
    "minecraft:jungle_leaves": _rgb(0x4A8A2A),
    "minecraft:acacia_log": _rgb(0x6B4F33),
    "minecraft:acacia_planks": _rgb(0xB4763B),
    "minecraft:acacia_leaves": _rgb(0x5C8A32),
    "minecraft:dark_oak_log": _rgb(0x3F2D1B),
    "minecraft:dark_oak_planks": _rgb(0x66442A),
    "minecraft:dark_oak_leaves": _rgb(0x33551F),
    "minecraft:mangrove_log": _rgb(0x5A3B2D),
    "minecraft:mangrove_planks": _rgb(0x7C3F32),
    "minecraft:mangrove_leaves": _rgb(0x3F6B2C),
    "minecraft:cherry_log": _rgb(0x4B2A2E),
    "minecraft:cherry_planks": _rgb(0xC9A0A0),
    "minecraft:cherry_leaves": _rgb(0xD98BB5),
    "minecraft:bamboo_block": _rgb(0x8C9C3E),
    "minecraft:crimson_stem": _rgb(0x6A2A3C),
    "minecraft:crimson_planks": _rgb(0x6A2A3C),
    "minecraft:crimson_nylium": _rgb(0x8A2530),
    "minecraft:warped_stem": _rgb(0x2E5560),
    "minecraft:warped_planks": _rgb(0x2E5560),
    "minecraft:warped_nylium": _rgb(0x2E7C67),
    # ores / valuable blocks
    "minecraft:coal_ore": _rgb(0x6B6B6B),
    "minecraft:iron_ore": _rgb(0x9C8878),
    "minecraft:copper_ore": _rgb(0x90806B),
    "minecraft:gold_ore": _rgb(0xA8904E),
    "minecraft:redstone_ore": _rgb(0x8A5A5A),
    "minecraft:lapis_ore": _rgb(0x4A648C),
    "minecraft:diamond_ore": _rgb(0x5A9A96),
    "minecraft:emerald_ore": _rgb(0x4C9464),
    "minecraft:ancient_debris": _rgb(0x5E4238),
    "minecraft:coal_block": _rgb(0x2E2E2E),
    "minecraft:iron_block": _rgb(0xD8D8D8),
    "minecraft:copper_block": _rgb(0xB87350),
    "minecraft:gold_block": _rgb(0xF2D24A),
    "minecraft:redstone_block": _rgb(0xB02020),
    "minecraft:lapis_block": _rgb(0x3352A6),
    "minecraft:diamond_block": _rgb(0x62E6D8),
    "minecraft:emerald_block": _rgb(0x3FD66B),
    "minecraft:netherite_block": _rgb(0x4A4244),
    "minecraft:quartz_block": _rgb(0xEDE7DE),
    # utility / base-build blocks
    "minecraft:crafting_table": _rgb(0x9A6B3C),
    "minecraft:furnace": _rgb(0x7A7A7A),
    "minecraft:blast_furnace": _rgb(0x6E7373),
    "minecraft:smoker": _rgb(0x6E5A48),
    "minecraft:chest": _rgb(0x8F6B32),
    "minecraft:trapped_chest": _rgb(0x8F6B32),
    "minecraft:barrel": _rgb(0x9A6E3C),
    "minecraft:hopper": _rgb(0x5A5A62),
    "minecraft:bookshelf": _rgb(0x9A7444),
    "minecraft:lectern": _rgb(0x9A7444),
    "minecraft:composter": _rgb(0x8A6F42),
    "minecraft:cauldron": _rgb(0x55555A),
    "minecraft:anvil": _rgb(0x55555A),
    "minecraft:enchanting_table": _rgb(0x7A3A5A),
    "minecraft:brewing_stand": _rgb(0x6C5A4A),
    "minecraft:beacon": _rgb(0x6FE3D8),
    "minecraft:conduit": _rgb(0x9AC0B8),
    "minecraft:respawn_anchor": _rgb(0x3A1E4A),
    "minecraft:lodestone": _rgb(0x7A7A80),
    "minecraft:nether_portal": _rgb(0x8A68C8),
    "minecraft:end_portal": _rgb(0x151C2E),
    "minecraft:end_portal_frame": _rgb(0x4C7A62),
    "minecraft:dragon_egg": _rgb(0x2A1A2E),
    "minecraft:tnt": _rgb(0xC03A2A),
    "minecraft:glass": _rgb(0xBFDCE6),
    "minecraft:glass_pane": _rgb(0xBFDCE6),
    "minecraft:iron_bars": _rgb(0x6A6A6E),
    "minecraft:ladder": _rgb(0x9A7444),
    "minecraft:scaffolding": _rgb(0xC2A464),
    "minecraft:hay_block": _rgb(0xD5B93A),
    "minecraft:pumpkin": _rgb(0xD07A2A),
    "minecraft:melon": _rgb(0x6C9C32),
    "minecraft:beehive": _rgb(0xB08A4A),
    "minecraft:bell": _rgb(0xD4B24A),
    "minecraft:campfire": _rgb(0x7A4A2A),
    "minecraft:jack_o_lantern": _rgb(0xD08430),
    "minecraft:shroomlight": _rgb(0xD08445),
    "minecraft:froglight": _rgb(0xE8E0B8),
    "minecraft:cake": _rgb(0xE8DCC8),
}
_WOOD_COLORS = {
    "oak": _rgb(0xB08A55), "birch": _rgb(0xC7B287), "spruce": _rgb(0x7D5A38),
    "jungle": _rgb(0xAD8558), "acacia": _rgb(0xB4763B), "dark_oak": _rgb(0x66442A),
    "mangrove": _rgb(0x7C3F32), "cherry": _rgb(0xC9A0A0), "bamboo": _rgb(0xC2A464),
    "crimson": _rgb(0x6A2A3C), "warped": _rgb(0x2E5560),
}
_LEAF_COLORS = {
    "oak": _rgb(0x4E7B2E), "birch": _rgb(0x6E9240), "spruce": _rgb(0x35592A),
    "jungle": _rgb(0x4A8A2A), "acacia": _rgb(0x5C8A32), "dark_oak": _rgb(0x33551F),
    "mangrove": _rgb(0x3F6B2C), "cherry": _rgb(0xD98BB5), "azalea": _rgb(0x5A7F38),
}
_DYE_COLORS = {
    "white": _rgb(0xE9ECEC), "orange": _rgb(0xF07613), "magenta": _rgb(0xBD44B3),
    "light_blue": _rgb(0x3AAFD9), "yellow": _rgb(0xF8C627), "lime": _rgb(0x70B919),
    "pink": _rgb(0xED8DAC), "gray": _rgb(0x3E4447), "light_gray": _rgb(0x8E8E86),
    "cyan": _rgb(0x158991), "purple": _rgb(0x792AAC), "blue": _rgb(0x35399D),
    "brown": _rgb(0x724728), "green": _rgb(0x546D1B), "red": _rgb(0xA12722),
    "black": _rgb(0x141519),
}
_ORE_COLORS = {
    "coal": _rgb(0x4A4A4A), "iron": _rgb(0xBF9C7E), "copper": _rgb(0xA97F5E),
    "gold": _rgb(0xE0BC4A), "redstone": _rgb(0xB03030), "lapis": _rgb(0x3A5AA8),
    "diamond": _rgb(0x5FD8D0), "emerald": _rgb(0x45C46A), "quartz": _rgb(0xEDE7DE),
}
_COLOR_WORD_SUFFIXES = (
    ("wool", 0.94), ("concrete_powder", 0.92), ("concrete", 1.0), ("terracotta", 0.78),
    ("glazed_terracotta", 0.82), ("stained_glass_pane", 1.04), ("stained_glass", 1.04),
    ("shulker_box", 0.9), ("carpet", 0.94), ("banner", 0.9), ("bed", 0.9), ("candle", 0.96),
)
_FALLBACK_PALETTE = (
    _rgb(0x7E8C6A), _rgb(0x7A6A58), _rgb(0x6A7A88), _rgb(0x88786A),
    _rgb(0x6E6A80), _rgb(0x8A8070), _rgb(0x5E7260), _rgb(0x84705E),
)
_UNKNOWN = object()


def _shade(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(channel * factor + 0.5))) for channel in color)


def _resolve_color(name: str):
    """Return (rgb, known) for a block id; unknown ids use a stable hash colour."""
    value = BLOCK_COLORS.get(name)
    if value is not None:
        return value, True
    short = name[len("minecraft:"):] if name.startswith("minecraft:") else name
    if short.endswith("_leaves"):
        wood = short[:-len("_leaves")]
        return _LEAF_COLORS.get(wood, _rgb(0x4E7B2E)), True
    if short.endswith(("_log", "_wood", "_hyphae", "_stem")):
        wood = short.rsplit("_", 1)[0]
        return _shade(_WOOD_COLORS.get(wood, _rgb(0x6B5334)), 0.9), True
    if short.endswith("_planks"):
        wood = short[:-len("_planks")]
        return _WOOD_COLORS.get(wood, _rgb(0xB08A55)), True
    if short.endswith("_sapling"):
        wood = short[:-len("_sapling")]
        return _shade(_LEAF_COLORS.get(wood, _rgb(0x4E7B2E)), 0.8), True
    if short.endswith("_ore"):
        base = short[:-len("_ore")]
        if base.startswith("deepslate_"):
            base = base[len("deepslate_"):]
        return _ORE_COLORS.get(base, _rgb(0x7A7A7A)), True
    if short.endswith(("_block", "_bricks", "_tiles")):
        return _rgb(0x8A8A8A), False
    for suffix, factor in _COLOR_WORD_SUFFIXES:
        if short.endswith("_" + suffix):
            dye = short[:-len(suffix) - 1]
            color = _DYE_COLORS.get(dye)
            if color is not None:
                return _shade(color, factor), True
    if short.endswith(("_slab", "_stairs", "_wall", "_fence", "_fence_gate", "_door", "_trapdoor",
                       "_button", "_pressure_plate", "_pane", "_sign", "_campfire", "_candle_cake")):
        base = short.rsplit("_", 1)[0]
        if base.startswith("waxed_"):
            base = base[len("waxed_"):]
        candidate = "minecraft:" + base
        resolved, known = _resolve_color(candidate)
        if known:
            return resolved, True
    if short.endswith("_coral"):
        return _rgb(0xE07070), False
    if short.endswith("_coral_block"):
        return _rgb(0xB05858), False
    if short.endswith("_coral_fan") or short.endswith("_coral_wall_fan"):
        return _rgb(0xE08080), False
    digest = hashlib.sha256(name.encode("utf-8", "replace")).digest()
    return _FALLBACK_PALETTE[digest[0] % len(_FALLBACK_PALETTE)], False


def color_for_block(name: str) -> tuple[int, int, int]:
    """Deterministic RGB map colour for a namespaced block id."""
    if not isinstance(name, str) or not name:
        raise ValueError("block name must be a non-empty string")
    return _resolve_color(name)[0]


# ---------------------------------------------------------------------------
# Bounded NBT reading (chunk subset).

class _NbtReader:
    __slots__ = ("raw", "pos", "items")
    MAX_DEPTH = 32
    MAX_ITEMS = 2_000_000
    MAX_STRING = 32767

    def __init__(self, raw: bytes):
        self.raw = raw
        self.pos = 0
        self.items = 0

    def take(self, size: int) -> bytes:
        if size < 0 or size > len(self.raw) - self.pos:
            raise ValueError("truncated chunk NBT")
        start = self.pos
        self.pos += size
        return self.raw[start:self.pos]

    def u8(self) -> int:
        return self.take(1)[0]

    def number(self, fmt: str):
        size = struct.calcsize(fmt)
        return struct.unpack(fmt, self.take(size))[0]

    def text(self) -> str:
        length = self.number(">H")
        if length > self.MAX_STRING:
            raise ValueError("chunk NBT string exceeds limit")
        return self.take(length).decode("utf-8", "replace")

    def skip(self, kind: int, depth: int = 0) -> None:
        self.items += 1
        if depth > self.MAX_DEPTH or self.items > self.MAX_ITEMS:
            raise ValueError("chunk NBT exceeds structural limits")
        if kind in {1, 2, 3, 4, 5, 6}:
            self.take({1: 1, 2: 2, 3: 4, 4: 8, 5: 4, 6: 8}[kind])
        elif kind == 8:
            self.take(self.number(">H"))
        elif kind == 10:
            while (tag := self.u8()) != 0:
                self.take(self.number(">H"))
                self.skip(tag, depth + 1)
        elif kind == 9:
            element = self.u8()
            count = self.number(">i")
            if not 0 <= count <= self.MAX_ITEMS:
                raise ValueError("invalid chunk NBT list length")
            for _ in range(count):
                self.skip(element, depth + 1)
        elif kind == 7:
            count = self.number(">i")
            if not 0 <= count <= self.MAX_ITEMS:
                raise ValueError("invalid chunk NBT byte array length")
            self.take(count)
        elif kind in {11, 12}:
            count = self.number(">i")
            if not 0 <= count <= self.MAX_ITEMS:
                raise ValueError("invalid chunk NBT array length")
            self.take(count * (4 if kind == 11 else 8))
        else:
            raise ValueError(f"unsupported chunk NBT type {kind}")

    def fields(self):
        """Yield ``(name, kind)`` for every field of the current compound."""
        while (kind := self.u8()) != 0:
            yield self.text(), kind


def _read_long_array(reader: _NbtReader) -> tuple[int, ...]:
    count = reader.number(">i")
    if not 0 <= count <= 4096:
        raise ValueError("block-state long array exceeds limit")
    if count == 0:
        return ()
    raw = reader.take(count * 8)
    return tuple(value & 0xFFFFFFFFFFFFFFFF for value in struct.unpack(">%dq" % count, raw))


def _read_block_states(reader: _NbtReader) -> dict:
    palette: list[str] = []
    data: tuple[int, ...] = ()
    for name, kind in reader.fields():
        if name == "palette":
            if kind != 9:
                raise ValueError("block-state palette is not a list")
            element = reader.u8()
            count = reader.number(">i")
            if element != 10 or not 1 <= count <= 4096:
                raise ValueError("invalid block-state palette")
            for _ in range(count):
                entry = {}
                for field, field_kind in reader.fields():
                    if field == "Name":
                        if field_kind != 8:
                            raise ValueError("block-state name is not a string")
                        entry["Name"] = reader.text()
                    else:
                        reader.skip(field_kind)
                name_value = entry.get("Name")
                if not isinstance(name_value, str) or not name_value or len(name_value) > 256:
                    raise ValueError("invalid block-state name")
                palette.append(name_value)
        elif name == "data":
            if kind != 12:
                raise ValueError("block-state data is not a long array")
            data = _read_long_array(reader)
        else:
            reader.skip(kind)
    return {"palette": tuple(palette), "data": data}


def _read_section(reader: _NbtReader) -> dict:
    y = None
    states = None
    for name, kind in reader.fields():
        if name == "Y":
            if kind != 1:
                raise ValueError("section Y is not a byte")
            y = reader.number(">b")
        elif name == "block_states":
            if kind != 10:
                raise ValueError("section block_states is not a compound")
            states = _read_block_states(reader)
        else:
            reader.skip(kind)
    if y is None or not -32 <= y <= 64:
        raise ValueError("section is missing a plausible Y")
    return {"y": y, "block_states": states if states is not None else {"palette": (), "data": ()}}


def _read_sections(reader: _NbtReader) -> list[dict]:
    element = reader.u8()
    count = reader.number(">i")
    if element != 10 or not 0 <= count <= 64:
        raise ValueError("invalid section list")
    return [_read_section(reader) for _ in range(count)]


def parse_chunk_nbt(raw: bytes) -> dict:
    """Parse the subset of a 1.20.4 chunk NBT payload used by the renderer."""
    if len(raw) > MAX_CHUNK_NBT:
        raise ValueError("chunk NBT exceeds size limit")
    reader = _NbtReader(raw)
    if reader.u8() != 10:
        raise ValueError("chunk NBT root is not a compound")
    reader.text()
    result = {"data_version": None, "x": None, "z": None, "status": None, "sections": []}
    wanted = {"DataVersion", "xPos", "zPos", "Status", "sections"}
    while (kind := reader.u8()) != 0:
        name = reader.text()
        if name not in wanted:
            reader.skip(kind)
            continue
        if name == "DataVersion":
            if kind != 3:
                raise ValueError("DataVersion is not an int")
            result["data_version"] = reader.number(">i")
        elif name in {"xPos", "zPos"}:
            if kind != 3:
                raise ValueError(f"{name} is not an int")
            result["x" if name == "xPos" else "z"] = reader.number(">i")
        elif name == "Status":
            if kind != 8:
                raise ValueError("Status is not a string")
            result["status"] = reader.text()
        else:
            result["sections"] = _read_sections(reader)
    if not result["sections"]:
        raise ValueError("chunk has no sections")
    return result


_BITS = {palette_size: max(4, (palette_size - 1).bit_length()) for palette_size in range(1, 4097)}


def decode_block_states(data: tuple[int, ...], palette_size: int) -> list[int] | None:
    """Decode 4096 compact-packed block-state indices (None for a single palette entry)."""
    if not 1 <= palette_size <= 4096:
        raise ValueError("block-state palette size is out of range")
    if palette_size == 1:
        return None
    bits = _BITS[palette_size]
    per_long = 64 // bits
    required = (4096 + per_long - 1) // per_long
    if len(data) < required:
        raise ValueError("block-state long array is too short for its palette")
    mask = (1 << bits) - 1
    indices = [0] * 4096
    for i in range(4096):
        indices[i] = (data[i // per_long] >> ((i % per_long) * bits)) & mask
    return indices


def _coerce_palette(name: str) -> str:
    if not name.startswith("minecraft:"):
        return "minecraft:" + name
    return name


def chunk_surface(chunk: dict) -> tuple[list, list, list]:
    """Return (top block names, terrain heights, top block heights) for 256 columns.

    Columns are stored ``z * 16 + x`` local indexes.  ``None`` means the column
    could not be resolved because the chunk NBT is incomplete.
    """
    top: list = [None] * 256
    height: list = [None] * 256
    top_y: list = [None] * 256
    sections = sorted(chunk["sections"], key=lambda section: -section["y"])
    for section in sections:
        states = section["block_states"]
        palette = tuple(_coerce_palette(entry) for entry in states["palette"])
        if not palette:
            continue
        y0 = section["y"] * 16
        indices = decode_block_states(states["data"], len(palette))
        if indices is None:
            name = palette[0]
            if name in AIR or name in DECORATION:
                continue
            leaf = name in LEAFY
            for column in range(256):
                if top[column] is None:
                    top[column] = name
                    top_y[column] = y0 + 15
                if height[column] is None and not leaf:
                    height[column] = y0 + 15
            if all(value is not None for value in top) and all(value is not None for value in height):
                break
            continue
        if max(indices) >= len(palette):
            raise ValueError("block-state palette index out of range")
        for i in range(4095, -1, -1):
            column = ((i >> 4) & 15) * 16 + (i & 15)
            name = palette[indices[i]]
            if name in AIR or name in DECORATION:
                continue
            if top[column] is None:
                top[column] = name
                top_y[column] = y0 + (i >> 8)
            if height[column] is None and name not in LEAFY:
                height[column] = y0 + (i >> 8)
            if all(value is not None for value in top) and all(value is not None for value in height):
                break
        if all(value is not None for value in top) and all(value is not None for value in height):
            break
    for column in range(256):
        if height[column] is None:
            height[column] = top_y[column]
    return top, height, top_y


# ---------------------------------------------------------------------------
# Region file reading.

class _Region:
    """A bounded read-only view of one ``.mca`` region file."""

    __slots__ = ("path", "stream", "header", "size")

    def __init__(self, path: Path):
        if path.is_symlink():
            raise WorldRenderError(f"region file must not be a symlink: {path}")
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError as exc:
            raise WorldRenderError(f"cannot open region file {path}: {exc}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise WorldRenderError(f"region file is not a regular file: {path}")
            self.size = metadata.st_size
            if not MIN_REGION_BYTES <= self.size <= MAX_REGION_BYTES:
                raise WorldRenderError(f"region file size is out of bounds: {path}")
            self.stream = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        self.path = path
        try:
            self.header = self.stream.read(MAX_REGION_HEADER)
        except OSError as exc:
            self.stream.close()
            raise WorldRenderError(f"cannot read region file header {path}: {exc}") from exc
        if len(self.header) != MAX_REGION_HEADER:
            self.stream.close()
            raise WorldRenderError(f"region file header is truncated: {path}")

    def close(self) -> None:
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def chunk_payload(self, chunk_x: int, chunk_z: int) -> bytes | None:
        """Return the decompressed chunk NBT, or None when the chunk is absent."""
        index = ((chunk_x % 32) + (chunk_z % 32) * 32) * 4
        location = struct.unpack_from(">I", self.header, index)[0]
        offset, sectors = location >> 8, location & 0xFF
        if location == 0:
            return None
        if offset < 2 or sectors == 0 or sectors > MAX_ARCHIVE_SECTORS:
            raise ValueError("invalid chunk sector location")
        start = offset * 4096
        if start + 5 > self.size:
            raise ValueError("chunk sector offset lies beyond the region file")
        padding = min(sectors * 4096, self.size - start)
        self.stream.seek(start)
        block = self.stream.read(padding)
        if len(block) < 5:
            raise ValueError("chunk sector payload is truncated")
        length, compression = struct.unpack_from(">IB", block, 0)
        if length < 1 or 4 + length > len(block):
            raise ValueError("chunk length exceeds its sector allocation")
        payload = block[5:4 + length]
        return _decompress_chunk(payload, compression)


def _decompress_chunk(payload: bytes, compression: int) -> bytes:
    if compression & 0x80:
        raise ValueError("external chunk files (.mcc) are not supported")
    if compression == 1:
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
                raw = stream.read(MAX_CHUNK_NBT + 1)
        except (OSError, EOFError, zlib.error) as exc:
            raise ValueError(f"corrupt gzip chunk: {exc}") from exc
        if len(raw) > MAX_CHUNK_NBT:
            raise ValueError("decompressed chunk exceeds size limit")
        return raw
    if compression == 2:
        decompressor = zlib.decompressobj()
        try:
            raw = decompressor.decompress(payload, MAX_CHUNK_NBT + 1)
        except zlib.error as exc:
            raise ValueError(f"corrupt zlib chunk: {exc}") from exc
        if len(raw) > MAX_CHUNK_NBT:
            raise ValueError("decompressed chunk exceeds size limit")
        if not decompressor.eof:
            raise ValueError("truncated or oversized zlib chunk")
        return raw
    if compression == 3:
        if len(payload) > MAX_CHUNK_NBT:
            raise ValueError("uncompressed chunk exceeds size limit")
        return payload
    if compression == 4:
        raise ValueError("LZ4 chunk compression is not supported")
    raise ValueError(f"unsupported chunk compression id {compression}")


# ---------------------------------------------------------------------------
# Rendering.

def _fill_rect(pixels: bytearray, width: int, height: int,
               x: int, y: int, rect_w: int, rect_h: int, color: tuple[int, int, int]) -> None:
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(width, x + rect_w), min(height, y + rect_h)
    if x1 <= x0 or y1 <= y0:
        return
    row = bytes(color) * (x1 - x0)
    for row_index in range(y0, y1):
        start = (row_index * width + x0) * 3
        pixels[start:start + len(row)] = row


def _set_pixel(pixels: bytearray, width: int, height: int,
               x: int, y: int, color: tuple[int, int, int]) -> None:
    if 0 <= x < width and 0 <= y < height:
        start = (y * width + x) * 3
        pixels[start:start + 3] = bytes(color)


def _line(pixels: bytearray, width: int, height: int,
          x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    error = dx - dy
    while True:
        _set_pixel(pixels, width, height, x0, y0, color)
        if x0 == x1 and y0 == y1:
            return
        doubled = 2 * error
        if doubled > -dy:
            error -= dy
            x0 += sx
        if doubled < dx:
            error += dx
            y0 += sy


def _frame_rect(pixels: bytearray, width: int, height: int,
                x: int, y: int, rect_w: int, rect_h: int,
                color: tuple[int, int, int], thickness: int = 1) -> None:
    for offset in range(thickness):
        _line(pixels, width, height, x + offset, y + offset, x + rect_w - 1 - offset, y + offset, color)
        _line(pixels, width, height, x + offset, y + rect_h - 1 - offset, x + rect_w - 1 - offset, y + rect_h - 1 - offset, color)
        _line(pixels, width, height, x + offset, y + offset, x + offset, y + rect_h - 1 - offset, color)
        _line(pixels, width, height, x + rect_w - 1 - offset, y + offset, x + rect_w - 1 - offset, y + rect_h - 1 - offset, color)


def _height_color(fraction: float) -> tuple[int, int, int]:
    """Blue-low to warm-white-high gradient for the relative height legend."""
    fraction = max(0.0, min(1.0, fraction))
    stops = ((0.0, (0x1E, 0x3A, 0x5A)), (0.35, (0x2F, 0x7A, 0x6E)), (0.7, (0xB8, 0xA8, 0x5A)), (1.0, (0xF4, 0xF4, 0xF0)))
    for (left, low), (right, high) in zip(stops, stops[1:]):
        if left <= fraction <= right:
            t = (fraction - left) / (right - left)
            return tuple(int(low[i] + (high[i] - low[i]) * t + 0.5) for i in range(3))
    return stops[-1][1]


def _block_color(top: str | None) -> tuple[int, int, int]:
    if top is None:
        return MISSING_COLOR
    return _resolve_color(top)[0]


def _fill_polygon(pixels: bytearray, width: int, height: int,
                  points: list[tuple[int, int]], color: tuple[int, int, int]) -> None:
    if not points:
        return
    ys = [point[1] for point in points]
    for y in range(max(0, min(ys)), min(height - 1, max(ys)) + 1):
        xs = []
        for index, (x1, y1) in enumerate(points):
            x2, y2 = points[(index + 1) % len(points)]
            if y1 == y2:
                if y == y1:
                    xs.extend((x1, x2))
                continue
            if min(y1, y2) <= y <= max(y1, y2):
                ratio = (y - y1) / (y2 - y1)
                xs.append(int(round(x1 + (x2 - x1) * ratio)))
        if len(xs) >= 2:
            _fill_rect(pixels, width, height, min(xs), y, max(xs) - min(xs) + 1, 1, color)


def _render_iso(tops: list, heights: list, *, sampled_w: int, sampled_d: int,
                x0: int, z0: int, step: int, width: int, height: int,
                spawn: tuple[int, int] | None, min_height: int, max_height: int) -> bytearray:
    """2.5D isometric column render from the same surface samples as the top map."""
    pixels = bytearray(bytes((13, 17, 23)) * (width * height))
    span = max(1, max_height - min_height)
    diagonal = max(1, sampled_w + sampled_d)
    width_bound = max(4, (width - 80) * 2 // diagonal)
    height_bound = max(4, (height - 240) * 4 // diagonal)
    tile = min(16, width_bound, height_bound)
    tile_h = max(2, tile // 2)
    level_px = max(1, tile // 5)
    origin_x = width // 2
    start_x = origin_x - (sampled_w - sampled_d) * tile // 4
    start_y = 70

    columns = []
    for j in range(sampled_d):
        for i in range(sampled_w):
            index = j * sampled_w + i
            top = tops[index]
            column_height = heights[index]
            if column_height is None:
                color = MISSING_COLOR
                column_height = min_height
            else:
                color = _block_color(top)
                color = _shade(color, 0.84 + 0.30 * ((column_height - min_height) / span))
            columns.append((i + j, i, j, color, column_height))
    columns.sort(key=lambda item: (item[0], item[2], item[1]))

    def screen(i: int, j: int, column_height: int) -> tuple[int, int]:
        sx = start_x + (i - j) * tile // 2
        sy = start_y + (i + j) * tile_h // 2 - (column_height - min_height) * level_px
        return sx, sy

    for _, i, j, color, column_height in columns:
        sx, sy = screen(i, j, column_height)
        top = [(sx, sy), (sx + tile // 2, sy + tile_h // 2),
               (sx, sy + tile_h), (sx - tile // 2, sy + tile_h // 2)]
        depth = max(2, (column_height - min_height + 1) * level_px)
        left = [(sx - tile // 2, sy + tile_h // 2), (sx, sy + tile_h),
                (sx, sy + tile_h + depth), (sx - tile // 2, sy + tile_h // 2 + depth)]
        right = [(sx, sy + tile_h), (sx + tile // 2, sy + tile_h // 2),
                 (sx + tile // 2, sy + tile_h // 2 + depth), (sx, sy + tile_h + depth)]
        _fill_polygon(pixels, width, height, left, _shade(color, 0.58))
        _fill_polygon(pixels, width, height, right, _shade(color, 0.38))
        _fill_polygon(pixels, width, height, top, color)

    if spawn:
        spawn_x, spawn_z = spawn
        i = (spawn_x - x0) // max(1, step)
        j = (spawn_z - z0) // max(1, step)
        if 0 <= i < sampled_w and 0 <= j < sampled_d:
            column_height = heights[j * sampled_w + i]
            if column_height is not None:
                sx, sy = screen(i, j, column_height)
                _line(pixels, width, height, sx, sy - 2, sx, sy - 14, (255, 255, 255))
                _line(pixels, width, height, sx, sy - 14, sx - 4, sy - 10, (255, 255, 255))
                _line(pixels, width, height, sx, sy - 14, sx + 4, sy - 10, (255, 255, 255))
    return pixels


def _render_top_down(tops: list, heights: list, *, sampled_w: int, sampled_d: int,
                     x0: int, z0: int, step: int, width: int, height: int,
                     spawn: tuple[int, int] | None, min_height: int, max_height: int) -> bytearray:
    pixels = bytearray(bytes((13, 17, 23)) * (width * height))
    margin = 20
    legend_gap = 40
    legend_need = 30  # bar width + tick length
    available_w = width - 2 * margin - legend_gap - legend_need
    available_h = height - 2 * margin
    if available_w <= 0 or available_h <= 0:
        raise WorldRenderError("output size is too small for the map frame")
    block_pixels = max(1, min(available_w // sampled_w, available_h // sampled_d))
    map_w = block_pixels * sampled_w
    map_h = block_pixels * sampled_d
    if map_w > available_w or map_h > available_h:
        raise WorldRenderError(
            "sampled area does not fit the chosen output size; increase size or reduce width/depth")
    composition_w = map_w + legend_gap + legend_need
    map_x = max(margin, (width - composition_w) // 2)
    map_y = margin + (available_h - map_h) // 2

    span = max(1, max_height - min_height)
    edge = 1 if block_pixels < 12 else 2

    def at(i: int, j: int):
        if 0 <= i < sampled_w and 0 <= j < sampled_d:
            index = j * sampled_w + i
            return tops[index], heights[index]
        return None, None

    for j in range(sampled_d):
        z = z0 + j * step
        for i in range(sampled_w):
            x = x0 + i * step
            index = j * sampled_w + i
            top = tops[index]
            column_height = heights[index]
            color = _block_color(top)
            if column_height is None:
                factor = 1.0
            else:
                relative = (column_height - min_height) / span
                factor = 0.78 + 0.30 * relative
                _, west = at(i - 1, j)
                _, north = at(i, j - 1)
                _, east = at(i + 1, j)
                _, south = at(i, j + 1)
                west = column_height if west is None else west
                north = column_height if north is None else north
                east = column_height if east is None else east
                south = column_height if south is None else south
                # light from the north-west: faces rising toward NW are brighter.
                factor *= 1.0 + 0.05 * (west - column_height) + 0.05 * (north - column_height)
                factor *= 1.0 - 0.03 * (column_height - east) - 0.03 * (column_height - south)
                if top == "minecraft:water":
                    factor = 0.93 + 0.10 * relative
            factor = max(0.55, min(1.35, factor))
            base = _shade(color, factor)
            px = map_x + i * block_pixels
            py = map_y + j * block_pixels
            _fill_rect(pixels, width, height, px, py, block_pixels, block_pixels, base)
            # side bevels: the visible south and east faces of a taller column.
            if block_pixels >= 3 and column_height is not None:
                _, east = at(i + 1, j)
                _, south = at(i, j + 1)
                side = _shade(base, 0.74)
                if east is not None and column_height > east:
                    _fill_rect(pixels, width, height, px + block_pixels - edge, py, edge, block_pixels, side)
                if south is not None and column_height > south:
                    _fill_rect(pixels, width, height, px, py + block_pixels - edge, block_pixels, edge, side)

    # Frame and relative-coordinate ticks: every 16 world blocks, longer every 32.
    frame = (78, 94, 116)
    tick = (150, 170, 192)
    _frame_rect(pixels, width, height, map_x - 1, map_y - 1, map_w + 2, map_h + 2, frame, 2)
    for block in range(0, sampled_w * step + 1, 16):
        column = int(round(block / step * block_pixels))
        if column > map_w:
            break
        length = 7 if block % 32 == 0 else 4
        _line(pixels, width, height, map_x + column, map_y - 1, map_x + column, map_y - 1 - length, tick)
        _line(pixels, width, height, map_x + column, map_y + map_h, map_x + column, map_y + map_h + length, tick)
    for block in range(0, sampled_d * step + 1, 16):
        offset = int(round(block / step * block_pixels))
        if offset > map_h:
            break
        length = 7 if block % 32 == 0 else 4
        _line(pixels, width, height, map_x - 1, map_y + offset, map_x - 1 - length, map_y + offset, tick)
        _line(pixels, width, height, map_x + map_w, map_y + offset, map_x + map_w + length, map_y + offset, tick)

    # Spawn marker (small white diamond with a red centre) when inside the map.
    if spawn is not None:
        spawn_x, spawn_z = spawn
        if x0 <= spawn_x < x0 + sampled_w * step and z0 <= spawn_z < z0 + sampled_d * step:
            sx = map_x + int(round((spawn_x - x0) / step * block_pixels + block_pixels / 2))
            sy = map_y + int(round((spawn_z - z0) / step * block_pixels + block_pixels / 2))
            white = (244, 244, 244)
            for dx, dy in ((0, -3), (0, 3), (-3, 0), (3, 0), (0, -2), (0, 2), (-2, 0), (2, 0)):
                _set_pixel(pixels, width, height, sx + dx, sy + dy, white)
            _set_pixel(pixels, width, height, sx, sy, (220, 60, 60))

    # Height bar: relative height gradient + ticks every 8 blocks.
    legend_x = map_x + map_w + legend_gap
    bar_w = 18
    bar_h = max(80, min(map_h // 2, 260))
    bar_y = map_y + max(0, (map_h - bar_h) // 2)
    for row in range(bar_h):
        fraction = 1.0 - row / max(1, bar_h - 1)
        _fill_rect(pixels, width, height, legend_x, bar_y + row, bar_w, 1, _height_color(fraction))
    _frame_rect(pixels, width, height, legend_x - 1, bar_y - 1, bar_w + 2, bar_h + 2, frame)
    for block in range(0, span + 1, HEIGHT_TICK_BLOCKS):
        offset = int(round(block / span * (bar_h - 1))) if span else 0
        y = bar_y + bar_h - 1 - offset
        length = 6 if block % (HEIGHT_TICK_BLOCKS * 2) == 0 else 3
        _line(pixels, width, height, legend_x + bar_w + 1, y, legend_x + bar_w + 1 + length, y, tick)

    # North arrow (up) and 16-block scale bar.
    arrow_x = legend_x + bar_w // 2
    arrow_y = bar_y - 34
    if arrow_y > margin + 8:
        _line(pixels, width, height, arrow_x, arrow_y + 14, arrow_x, arrow_y + 2, tick)
        _line(pixels, width, height, arrow_x, arrow_y, arrow_x - 5, arrow_y + 8, tick)
        _line(pixels, width, height, arrow_x, arrow_y, arrow_x + 5, arrow_y + 8, tick)
    scale_y = map_y + map_h + 24
    scale_x = map_x
    if scale_y < height - 6 and block_pixels * SCALE_BAR_BLOCKS <= map_w:
        scale_len = block_pixels * SCALE_BAR_BLOCKS
        _line(pixels, width, height, scale_x, scale_y, scale_x + scale_len, scale_y, tick)
        for offset in (0, scale_len // 2, scale_len):
            _line(pixels, width, height, scale_x + offset, scale_y - 4, scale_x + offset, scale_y + 4, tick)
    return pixels


def _encode_png(pixels: bytearray, width: int, height: int) -> bytes:
    if len(pixels) != width * height * 3:
        raise ValueError("pixel buffer size does not match image size")
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")


# ---------------------------------------------------------------------------
# World inspection helpers.

def _absolute_no_symlinks(value) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    current = Path(raw.anchor)
    for part in raw.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise WorldRenderError(f"symlinked paths are not permitted: {current}")
    return Path(os.path.abspath(raw))


def _world_path(world_dir) -> Path:
    if not isinstance(world_dir, (str, os.PathLike)):
        raise WorldRenderError("world_dir must be a filesystem path")
    world = _absolute_no_symlinks(world_dir)
    if not world.is_dir():
        raise WorldRenderError(f"world directory does not exist or is not a directory: {world}")
    level = world / "level.dat"
    if not level.is_file():
        raise WorldRenderError(f"world has no level.dat: {level}")
    if not (world / "region").is_dir() or (world / "region").is_symlink():
        raise WorldRenderError(f"world has no usable region directory: {world / 'region'}")
    return world


def _stat_signature(path: Path) -> dict:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        return {"error": str(exc)}
    return {"size": metadata.st_size, "mtime_ns": metadata.st_mtime_ns}


def world_fingerprint(world_dir) -> dict:
    """Cheap cache key for a world's mutable Anvil inputs (size + mtime)."""
    world = _world_path(world_dir)
    regions = {}
    for path in sorted((world / "region").glob("*.mca")):
        if path.is_symlink():
            continue
        regions[path.name] = _stat_signature(path)
    document = {"schema_version": SCHEMA_VERSION, "level_dat": _stat_signature(world / "level.dat"), "regions": regions}
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    document["sha256"] = hashlib.sha256(encoded).hexdigest()
    return document


def _read_level_identity(world: Path) -> dict:
    try:
        data = read_level(world / "level.dat")
    except (OSError, ValueError) as exc:
        raise WorldRenderError(f"cannot read level.dat: {exc}") from exc
    spawn_x = data.get("SpawnX", 0)
    spawn_y = data.get("SpawnY", 0)
    spawn_z = data.get("SpawnZ", 0)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in (spawn_x, spawn_y, spawn_z)):
        raise WorldRenderError("level.dat spawn coordinates are malformed")
    level_name = data.get("LevelName")
    if not isinstance(level_name, str) or not level_name:
        level_name = world.name
    version = data.get("Version", {}).get("Name") if isinstance(data.get("Version"), dict) else None
    identity = {
        "level_name": level_name,
        "spawn": {"x": spawn_x, "y": spawn_y, "z": spawn_z},
        "data_version": data.get("DataVersion"),
        "minecraft_version": version,
    }
    return identity


def _validate_arguments(width, depth, style, size, center):
    for label, value in (("width", width), ("depth", depth)):
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_DIMENSION:
            raise WorldRenderError(f"{label} must be an integer in [1, {MAX_DIMENSION}]")
    if style not in SUPPORTED_STYLES:
        raise WorldRenderError(f"unsupported render style {style!r}; expected one of {', '.join(SUPPORTED_STYLES)}")
    if not isinstance(size, (tuple, list)) or len(size) != 2:
        raise WorldRenderError("size must be a (pixel_width, pixel_height) pair")
    pixel_w, pixel_h = size
    if any(not isinstance(value, int) or isinstance(value, bool) or not 64 <= value <= 8192 for value in (pixel_w, pixel_h)):
        raise WorldRenderError("output size must be two integers in [64, 8192]")
    if center is None:
        return (pixel_w, pixel_h)
    if not isinstance(center, (tuple, list)) or len(center) != 2:
        raise WorldRenderError("center must be a (block_x, block_z) pair or None for spawn")
    normalized = []
    for value in center:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise WorldRenderError("center coordinates must be finite numbers")
        if abs(value) > 30_000_000:
            raise WorldRenderError("center coordinates lie outside the Minecraft world border")
        normalized.append(int(round(value)))
    return (pixel_w, pixel_h)


def _chunk_plan(bounds: tuple[int, int, int, int]) -> dict:
    x0, z0, x1, z1 = bounds
    chunk_x0, chunk_x1 = x0 >> 4, (x1 - 1) >> 4
    chunk_z0, chunk_z1 = z0 >> 4, (z1 - 1) >> 4
    plan: dict = {}
    requested = 0
    for chunk_x in range(chunk_x0, chunk_x1 + 1):
        for chunk_z in range(chunk_z0, chunk_z1 + 1):
            region = (chunk_x // 32, chunk_z // 32)
            plan.setdefault(region, []).append((chunk_x, chunk_z))
            requested += 1
    if requested > MAX_CHUNKS:
        raise WorldRenderError(
            f"requested area needs {requested} chunks, exceeding the {MAX_CHUNKS}-chunk render budget; "
            "reduce width/depth")
    return plan


def _sample_stride(width: int, depth: int) -> int:
    if width * depth <= MAX_COLUMNS:
        return 1
    return max(1, int(math.ceil(math.sqrt(width * depth / MAX_COLUMNS))))


def _write_output(output_path, data: bytes) -> Path:
    if not isinstance(output_path, (str, os.PathLike)):
        raise WorldRenderError("output_path must be a filesystem path")
    output = _absolute_no_symlinks(output_path)
    if output.exists() and output.is_symlink():
        raise WorldRenderError(f"output path must not be a symlink: {output}")
    if output.exists() and not output.is_file():
        raise WorldRenderError(f"output path is not a regular file: {output}")
    parent = output.parent
    if not parent.is_dir():
        parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("wb") as stream:
            stream.write(data)
    except OSError as exc:
        raise WorldRenderError(f"cannot write output image {output}: {exc}") from exc
    return output


def render_world(world_dir, output_path, *, center=None, width=DEFAULT_WIDTH, depth=DEFAULT_DEPTH,
                 style="top", size=DEFAULT_SIZE, full_world=False) -> dict:
    """Render a deterministic read-only block map PNG; return its metadata.

    ``world_dir`` is the saved overworld directory (the one containing
    ``level.dat`` and ``region/``).  ``center`` defaults to the spawn X/Z from
    ``level.dat``; ``width``/``depth`` are the sampled block extents in X/Z.
    ``output_path`` is the only file written.  Raises :class:`WorldRenderError`
    for unusable worlds or arguments; missing/corrupt chunks degrade to a
    ``"partial"`` image described in ``metadata["errors"]``.
    """
    if style == "mesh":
        from .world_mesh import render_mesh_world
        return render_mesh_world(world_dir, output_path, center=center, width=width,
                                 depth=depth, size=size, full_world=full_world)
    started = time.monotonic()
    generated_at = datetime.now(timezone.utc).isoformat()
    pixel_w, pixel_h = _validate_arguments(width, depth, style, size, center)
    world = _world_path(world_dir)
    identity = _read_level_identity(world)
    spawn = identity["spawn"]
    if center is None:
        center_x, center_z = spawn["x"], spawn["z"]
    else:
        center_x, center_z = int(round(center[0])), int(round(center[1]))
    x0 = center_x - width // 2
    z0 = center_z - depth // 2
    x1, z1 = x0 + width, z0 + depth
    plan = _chunk_plan((x0, z0, x1, z1))
    step = _sample_stride(width, depth)
    sampled_w = (width + step - 1) // step
    sampled_d = (depth + step - 1) // step
    fingerprint_before = world_fingerprint(world)
    region_dir = world / "region"

    # Map every sampled position to its chunk once; fill it from each chunk read.
    positions: dict = {}
    total_columns = sampled_w * sampled_d
    tops: list = [None] * total_columns
    heights: list = [None] * total_columns
    for j in range(sampled_d):
        z = z0 + j * step
        for i in range(sampled_w):
            x = x0 + i * step
            positions.setdefault(((x >> 4), (z >> 4)), []).append(
                (j * sampled_w + i, (z & 15) * 16 + (x & 15)))

    chunks_read = 0
    chunks_missing = 0
    chunks_corrupt = 0
    sections_read = 0
    data_versions: dict[str, int] = {}
    errors: list[str] = []
    unresolved = 0

    def note_error(message: str) -> None:
        if len(errors) < MAX_REPORTED_ERRORS:
            errors.append(message)

    region_paths = {}
    for region_key in plan:
        region_x, region_z = region_key
        path = region_dir / f"r.{region_x}.{region_z}.mca"
        if path.is_symlink():
            raise WorldRenderError(f"region file must not be a symlink: {path}")
        region_paths[region_key] = path

    for region_key, chunk_coords in plan.items():
        path = region_paths[region_key]
        if not path.is_file():
            chunks_missing += len(chunk_coords)
            note_error(f"region file missing: {path.name} ({len(chunk_coords)} chunks)")
            continue
        try:
            region = _Region(path)
        except WorldRenderError:
            raise
        try:
            for chunk_x, chunk_z in chunk_coords:
                try:
                    payload = region.chunk_payload(chunk_x, chunk_z)
                except ValueError as exc:
                    chunks_corrupt += 1
                    note_error(f"chunk {chunk_x},{chunk_z}: {exc}")
                    continue
                if payload is None:
                    chunks_missing += 1
                    note_error(f"chunk {chunk_x},{chunk_z}: not generated in {path.name}")
                    continue
                try:
                    chunk = parse_chunk_nbt(payload)
                except ValueError as exc:
                    chunks_corrupt += 1
                    note_error(f"chunk {chunk_x},{chunk_z}: {exc}")
                    continue
                if chunk.get("x") != chunk_x or chunk.get("z") != chunk_z:
                    chunks_corrupt += 1
                    note_error(f"chunk {chunk_x},{chunk_z}: coordinate mismatch in chunk NBT")
                    continue
                chunks_read += 1
                version = chunk.get("data_version")
                if isinstance(version, int):
                    data_versions[str(version)] = data_versions.get(str(version), 0) + 1
                sections_read += len(chunk["sections"])
                try:
                    chunk_tops, chunk_heights, _ = chunk_surface(chunk)
                except ValueError as exc:
                    chunks_corrupt += 1
                    note_error(f"chunk {chunk_x},{chunk_z}: {exc}")
                    continue
                for index, column in positions.get((chunk_x, chunk_z), ()):
                    top = chunk_tops[column]
                    if top is None:
                        continue
                    tops[index] = top
                    heights[index] = chunk_heights[column]
        finally:
            region.close()
    unresolved = sum(1 for value in tops if value is None)
    if chunks_read == 0:
        detail = "; ".join(errors[:3]) or "no chunks were found in the requested area"
        raise WorldRenderError(
            f"no readable chunks for the requested area ({detail})")

    resolved_heights = [value for value in heights if value is not None]
    if not resolved_heights:
        raise WorldRenderError("chunk data did not yield any resolvable block columns")
    min_height, max_height = min(resolved_heights), max(resolved_heights)

    if style == "iso":
        pixels = _render_iso(
            tops, heights, sampled_w=sampled_w, sampled_d=sampled_d, x0=x0, z0=z0, step=step,
            width=pixel_w, height=pixel_h, spawn=(spawn["x"], spawn["z"]),
            min_height=min_height, max_height=max_height)
    else:
        pixels = _render_top_down(
            tops, heights, sampled_w=sampled_w, sampled_d=sampled_d, x0=x0, z0=z0, step=step,
            width=pixel_w, height=pixel_h, spawn=(spawn["x"], spawn["z"]),
            min_height=min_height, max_height=max_height)
    png = _encode_png(pixels, pixel_w, pixel_h)
    output = _write_output(output_path, png)
    fingerprint_after = world_fingerprint(world)

    block_counts: dict[str, int] = {}
    unknown_blocks = set()
    for top in tops:
        if top is None:
            continue
        block_counts[top] = block_counts.get(top, 0) + 1
        if not _resolve_color(top)[1]:
            unknown_blocks.add(top)
    status = "ok" if not chunks_missing and not chunks_corrupt else "partial"
    height_range = max_height - min_height
    caption = (
        f"Read-only {style} block map of the saved Minecraft world '{identity['level_name']}' "
        f"around X {x0}..{x1 - 1}, Z {z0}..{z1 - 1} "
        f"({sampled_w}x{sampled_d} sampled columns at stride {step}, {pixel_w}x{pixel_h} px). "
        f"Colour = highest block per column; brightness = relative height "
        f"({min_height}..{max_height}, bar at right); ticks every 16 blocks; "
        f"white cross = spawn; north is up (negative Z).")
    method = (
        "Stdlib Anvil reader: region location table + bounded zlib/gzip chunk NBT; "
        "1.20.4 section block-state palettes decoded from compact packed longs; "
        "highest non-air block per sampled column, north-west height hill-shade and "
        "south/east side bevels; deterministic stdlib PNG encoder.")
    limitations = [
        "Only the overworld region/ directory is read; DIM-1/DIM1 saves are ignored.",
        "Requires 1.16+ compact block-state packing (DataVersion >= 2529); older chunk layouts are reported as corrupt.",
        "LZ4-compressed and external (.mcc) chunks are rejected as unsupported.",
        "No block entities, biomes, lighting, textures, transparency or true 3D meshing; a column shows one top block.",
        "Leaf canopies keep their leaf colour but do not lift the rendered terrain height.",
        "Missing/corrupt chunks render as dark unknown cells; an actively saving server can produce them.",
        "Decorative blocks (grass, flowers, torches, crops) are ignored for the surface; the ground below is shown.",
        "No text is drawn into the image; the dashboard supplies the caption from this metadata.",
    ]
    if step > 1:
        limitations.append(
            f"Area exceeds {MAX_COLUMNS} columns; sampled every {step} blocks (stride).")
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "renderer": RENDERER,
        "status": status,
        "style": "iso_2.5d" if style == "iso" else "top_down",
        "read_only": True,
        "world_dir": str(world),
        "level_name": identity["level_name"],
        "minecraft_version": identity["minecraft_version"],
        "spawn": spawn,
        "center": {"x": center_x, "z": center_z},
        "bounds": {"x0": x0, "x1": x1, "z0": z0, "z1": z1},
        "width": width,
        "depth": depth,
        "sample_stride": step,
        "sampled_columns": [sampled_w, sampled_d],
        "output": str(output),
        "size": [pixel_w, pixel_h],
        "png_bytes": len(png),
        "png_sha256": hashlib.sha256(png).hexdigest(),
        "chunks": {"requested": sum(len(value) for value in plan.values()),
                   "read": chunks_read, "missing": chunks_missing, "corrupt": chunks_corrupt},
        "sections_read": sections_read,
        "data_versions": dict(sorted(data_versions.items())),
        "relative_coordinates": {"origin_x": x0, "origin_z": z0,
                                 "ticks_every_blocks": 16, "north": "negative Z (up)",
                                 "spawn_marker": True},
        "height_frame": {"min": min_height, "max": max_height, "span": height_range,
                         "bar_ticks_every_blocks": HEIGHT_TICK_BLOCKS,
                         "scale_bar_blocks": SCALE_BAR_BLOCKS},
        "block_counts": dict(sorted(block_counts.items(), key=lambda item: (-item[1], item[0]))[:MAX_REPORTED_BLOCKS]),
        "distinct_top_blocks": len(block_counts),
        "unresolved_columns": unresolved,
        "unknown_blocks": sorted(unknown_blocks)[:MAX_REPORTED_UNKNOWN],
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
    return metadata
