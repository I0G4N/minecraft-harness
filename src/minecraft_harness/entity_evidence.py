"""Read-only entity evidence from the server's saved world.

Only End Crystal count/positions are extracted. A vanilla saved world does not
record which player destroyed a crystal; per-crystal attribution therefore
remains a player observation, not server truth.
"""
from __future__ import annotations

from pathlib import Path

from .environment import _NBT
from .world_mesh import _Region


def end_crystal_summary(world_dir: str | Path) -> dict:
    world = Path(world_dir)
    candidates = [world / "DIM1" / "entities"]
    existing = [path for path in candidates if path.is_dir() and not path.is_symlink()]
    if not existing:
        return {"available": False, "count": None, "positions": [],
                "method": "world/DIM1/entities not present; the End has not been saved or was not generated",
                "source": "server save"}
    positions = []
    chunks = 0
    errors = []
    for directory in existing:
        for path in sorted(directory.glob("*.mca")):
            if path.is_symlink():
                errors.append("entity region is a symlink")
                continue
            region = None
            try:
                region = _Region(path)
                for chunk_x in range(32):
                    for chunk_z in range(32):
                        payload = region.chunk_payload(chunk_x, chunk_z)
                        if not payload:
                            continue
                        chunks += 1
                        parser = _NBT(payload)
                        if parser.number(">B") != 10:
                            continue
                        parser.text()
                        document = parser.value(10)
                        entities = document.get("Entities") if isinstance(document, dict) else None
                        if not isinstance(entities, list):
                            continue
                        for entity in entities:
                            if not isinstance(entity, dict) or entity.get("id") != "minecraft:end_crystal":
                                continue
                            pos = entity.get("Pos")
                            if isinstance(pos, list) and len(pos) == 3 and all(
                                    isinstance(value, (int, float)) and not isinstance(value, bool)
                                    for value in pos):
                                positions.append([round(float(value), 3) for value in pos])
                            else:
                                positions.append(None)
            except (OSError, ValueError, EOFError) as exc:
                if len(errors) < 20:
                    errors.append(f"{path.name}: {exc}")
            finally:
                if region is not None:
                    try:
                        region.close()
                    except Exception:
                        pass
    if chunks == 0:
        return {"available": False, "count": None, "positions": [], "errors": errors,
                "method": "entity regions exist but no readable entity chunks",
                "source": "server save"}
    return {"available": True, "count": len(positions),
            "positions": [position for position in positions if position is not None],
            "errors": errors, "chunks_read": chunks,
            "method": "read-only world/DIM1/entities scan for minecraft:end_crystal",
            "source": "server save"}
