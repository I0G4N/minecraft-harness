"""Dynamic data for the paper-figure gallery.

Read-only view over a run ledger and its server evidence. Times are seconds
since the first program (the paper's Fig. 3/4 convention). Snapshot-derived
milestones are observation upper bounds and are labelled as such; agent claims
never establish a stage by themselves.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .evaluation.collector import artifact_bytes

SCHEMA_VERSION = 1
MAX_SERIES = 4000
STAGE_ORDER = (
    ("stone_tools", "Stone tools", "overworld"),
    ("iron_tools", "Iron tools", "overworld"),
    ("bucket", "Bucket", "overworld"),
    ("flint_and_steel", "Flint and steel", "overworld"),
    ("nether_portal", "Nether portal", "nether"),
    ("nether_fortress", "Nether fortress", "nether"),
    ("blaze_rods", "Blaze rods", "nether"),
    ("ender_pearls", "Ender pearls", "nether"),
    ("eyes_of_ender", "Eyes of Ender", "stronghold"),
    ("stronghold", "Stronghold", "stronghold"),
    ("the_end", "The End", "end"),
    ("ten_crystals", "Ten crystals", "end"),
    ("ender_dragon", "Ender Dragon", "end"),
)
STAGE_LABELS = {key: label for key, label, _ in STAGE_ORDER}
STAGE_DIMENSION = {key: dimension for key, _, dimension in STAGE_ORDER}

# The paper's own thirteen-step figure mixes an advancement ("Stone Age"),
# item counts stored in world statistics, and unobservable-in-advancements
# events (ten crystals). Each source is marked in the payload.
ADVANCEMENT_STAGE = {
    "minecraft:story/upgrade_tools": "stone_tools",
    "minecraft:story/obtain_iron_ingot": "iron_tools",
    "minecraft:story/enter_the_nether": "nether_portal",
    "minecraft:nether/find_fortress": "nether_fortress",
    "minecraft:nether/obtain_blaze_rod": "blaze_rods",
    "minecraft:story/follow_ender_eye": "stronghold",
    "minecraft:story/enter_the_end": "the_end",
    "minecraft:end/kill_dragon": "ender_dragon",
}
TITLE_STAGE = {
    "stone age": "stone_tools",
    "acquire hardware": "iron_tools",
    "we need to go deeper": "nether_portal",
    "a terrible fortress": "nether_fortress",
    "into fire": "blaze_rods",
    "eye spy": "stronghold",
    "the end?": "the_end",
    "free the end": "ender_dragon",
}
# (stage, stats category, item ids). The first snapshot in which any item is
# present is an observation upper bound; the source player is recorded.
STAT_STAGES = (
    ("stone_tools", "minecraft:crafted", ("minecraft:stone_pickaxe", "minecraft:stone_axe", "minecraft:stone_shovel", "minecraft:stone_sword")),
    ("iron_tools", "minecraft:crafted", ("minecraft:iron_pickaxe", "minecraft:iron_axe", "minecraft:iron_shovel", "minecraft:iron_sword")),
    ("bucket", "minecraft:crafted", ("minecraft:bucket",)),
    ("flint_and_steel", "minecraft:crafted", ("minecraft:flint_and_steel",)),
    ("ender_pearls", "minecraft:picked_up", ("minecraft:ender_pearl",)),
    ("eyes_of_ender", "minecraft:crafted", ("minecraft:ender_eye",)),
)
# Paper Figure 13's five message-pattern categories, reconstructed from the
# published bars: warnings, item rescues, shared chests, lessons/tips, exact coords.
PAPER_BEHAVIORS = (
    ("warnings", r"\b(danger|careful|creeper|lava|watch out|warning|heads up|do not|don't|avoid|incoming)\b"),
    ("rescues", r"\b(rescue|save (?:me|him|her|them)|help|stuck|trapped|bring .{0,20}back|recover|retrieve|items?)\b"),
    ("shared_chests", r"\b(chest|barrel|storage|deposit|withdraw|stored|drop ?box|bank)\b"),
    ("lessons", r"\b(lesson|tip|learned|learn from|remember|next time|note that|rule|strategy)\b"),
    ("coordinates", r"(?<![\w.])(?:-?\d{1,5}\s*,\s*-?\d{1,3}\s*,\s*-?\d{1,5})(?![\w.])"),
)
PROJECT_LABEL_TO_PAPER = {
    "warning": "warnings",
    "rescue": "rescues",
    "shared_storage": "shared_chests",
    "coordinates": "coordinates",
    "lesson": "lessons",
}
PAPER_DEATH_GROUPS = {
    "Monsters": {"combat", "explosion", "magic"},
    "The dragon": {"dragon_breath", "sonic_boom"},
    "Drowning": {"drowning"},
    "Falls and the void": {"void", "fall", "kinetic_energy", "falling_object"},
    "Lava and fire": {"lava", "fire", "hot_floor"},
}
AGENT_COLORS = ("#4a72b8", "#dc7a1f", "#2f8f6b", "#a33f9e")
TERMINALS = {"succeeded", "failed", "timed_out", "cancelled", "interrupted", "rejected", "invalidated"}
STATUSES = {"execution_accepted", "execution_running", "execution_submitted", "execution_finished",
            "execution_succeeded", "execution_failed", "execution_timed_out", "execution_cancelled",
            "execution_interrupted", "execution_rejected", "execution_invalidated"}


def _ts(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _json(raw):
    try:
        value = json.loads(raw)
        return value if isinstance(value, (dict, list)) else None
    except (ValueError, TypeError):
        return None


def _artifact(run_dir, digest):
    if not isinstance(digest, str) or not re.fullmatch("[a-f0-9]{64}", digest):
        return None
    try:
        return _json(artifact_bytes(run_dir, digest))
    except (OSError, ValueError, UnicodeError):
        return None


def _offline_uuid(agent):
    return str(uuid.UUID(bytes=hashlib.md5(("OfflinePlayer:" + agent).encode()).digest(), version=3))


def _clamp(value, origin):
    if value is None:
        return None
    if origin is None:
        return 0.0
    return max(0.0, round(value - origin, 3))


def _evidence(row, item=None):
    try:
        raw = row["payload_json"]
        event_seq = row["event_seq"]
    except (KeyError, IndexError):
        raw, event_seq = None, None
    payload = _json(raw or "{}") or {}
    source = (item or {}).get("source") or {}
    return {"event_seq": event_seq, "evidence_id": payload.get("evidence_id"),
            "artifact_sha256": payload.get("artifact_sha256"), "line": source.get("line")}


def _program_series(conn, origin):
    records = {}
    for row in conn.execute("SELECT event_seq, kind, actor_id, received_at, payload_json FROM events WHERE kind LIKE 'execution_%' ORDER BY event_seq"):
        if row["kind"] not in STATUSES:
            continue
        payload = _json(row["payload_json"]) or {}
        record = payload.get("record") if isinstance(payload.get("record"), dict) else payload
        execution_id = record.get("execution_id") or payload.get("execution_id")
        if not execution_id:
            continue
        agent = record.get("agent_id") or row["actor_id"] or "unknown"
        item = records.setdefault(execution_id, {"execution_id": execution_id, "agent": agent, "event_seq": row["event_seq"]})
        start = _ts(record.get("started_at") or record.get("accepted_at")) or _ts(row["received_at"])
        finish = _ts(record.get("finished_at"))
        status = record.get("status")
        suffix = row["kind"].removeprefix("execution_")
        if suffix in TERMINALS:
            status = suffix
        if start is not None:
            item["start"] = min(item.get("start", start), start)
        if finish is not None:
            item["finish"] = max(item.get("finish", finish), finish)
        if status:
            item["status"] = status
        item["event_seq"] = min(item.get("event_seq", row["event_seq"]), row["event_seq"])
    series = []
    for item in records.values():
        start = item.get("start")
        if start is None:
            continue
        finish = item.get("finish")
        series.append({"agent": item["agent"], "start": _clamp(start, origin),
                       "end": _clamp(finish if finish is not None else start, origin),
                       "status": item.get("status") or "unknown", "event_seq": item["event_seq"]})
    series.sort(key=lambda item: (item["start"], item["agent"]))
    return series


def _job_series(conn, origin):
    records = {}
    for row in conn.execute("SELECT event_seq, kind, actor_id, payload_json FROM events WHERE kind LIKE 'job_%' ORDER BY event_seq"):
        payload = _json(row["payload_json"]) or {}
        record = payload.get("job") if isinstance(payload.get("job"), dict) else payload
        job_id = record.get("job_id") or payload.get("job_id")
        if not job_id:
            continue
        item = records.setdefault(job_id, {"agent": record.get("agent_id") or row["actor_id"] or "unknown", "event_seq": row["event_seq"]})
        start = _ts(record.get("started_at"))
        finish = _ts(record.get("finished_at"))
        if start is not None:
            item["start"] = min(item.get("start", start), start)
        if finish is not None:
            item["finish"] = max(item.get("finish", finish), finish)
        item["status"] = record.get("status") or item.get("status")
    out = []
    for item in records.values():
        start = item.get("start")
        if start is None:
            continue
        out.append({"agent": item["agent"], "start": _clamp(start, origin),
                    "end": _clamp(item.get("finish") or start, origin),
                    "status": item.get("status") or "unknown", "event_seq": item["event_seq"]})
    out.sort(key=lambda item: item["start"])
    return out


def _server_items(conn, run_dir):
    """Normalized server-log facts (chat/death/advancement) from evidence artifacts."""
    chats, deaths, advancements, memory_events = [], [], [], []
    omitted = Counter()
    for row in conn.execute("SELECT event_seq, payload_json, received_at FROM events WHERE kind='server_log_segment' ORDER BY event_seq"):
        payload = _json(row["payload_json"]) or {}
        data = _artifact(run_dir, payload.get("artifact_sha256"))
        if not isinstance(data, dict):
            omitted["server_log_artifact_unavailable"] += 1
            continue
        for item in data.get("events") or []:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            if kind not in {"chat", "death", "advancement"}:
                continue
            when = _ts(item.get("source_time") or item.get("received_at") or row["received_at"])
            if when is None:
                omitted["server_item_without_time"] += 1
                continue
            actor = item.get("actor_id") or "unknown"
            ref = _evidence(row, item)
            if kind == "chat":
                target, text = chats, (item.get("message") or item.get("raw_message") or "")
                value = {"t": when, "agent": actor, "text": str(text)[:4000],
                         "labels": (item.get("classification") or {}).get("labels") or [], "evidence": ref}
            elif kind == "death":
                target, text = deaths, (item.get("raw_message") or "")
                value = {"t": when, "agent": actor, "cause": item.get("cause") or "unknown",
                         "text": str(text)[:4000], "evidence": ref}
            else:
                target, text = advancements, (item.get("raw_message") or "")
                value = {"t": when, "agent": actor, "title": item.get("title") or "",
                         "canonical_id": item.get("canonical_id"), "text": str(text)[:4000], "evidence": ref}
            target.append(value)
    return chats, deaths, advancements, omitted


def _snapshot_stages(conn, run_dir, agents, origin):
    """Detect the thirteen paper stages from world snapshots and log titles."""
    by_uuid = {_offline_uuid(agent): agent for agent in agents}
    candidates = defaultdict(list)
    snapshots = list(conn.execute("SELECT event_seq, payload_json, received_at FROM events WHERE kind='server_world_snapshot' ORDER BY event_seq"))
    baseline_seen = False
    for row in snapshots:
        payload = _json(row["payload_json"]) or {}
        snapshot = _artifact(run_dir, payload.get("artifact_sha256"))
        if not isinstance(snapshot, dict):
            continue
        baseline = bool(payload.get("baseline"))
        observed = _ts(row["received_at"])
        for relative, digest in (snapshot.get("files") or {}).items():
            if not isinstance(relative, str):
                continue
            player_uuid = Path(relative).stem
            agent = by_uuid.get(player_uuid, agents[0] if len(agents) == 1 else player_uuid)
            if relative.startswith("advancements/"):
                document = _artifact(run_dir, digest)
                if not isinstance(document, dict):
                    continue
                for advancement_id, entry in document.items():
                    stage = ADVANCEMENT_STAGE.get(advancement_id)
                    if not stage or not isinstance(entry, dict) or entry.get("done") is not True:
                        continue
                    times = [_ts(value) for value in (entry.get("criteria") or {}).values() if isinstance(value, str)]
                    times = [value for value in times if value is not None]
                    when = max(times) if times else observed
                    precision = "advancement_criteria" if times else "snapshot_observation_upper_bound"
                    if baseline:
                        when, precision = origin, "baseline"
                    candidates[stage].append({"t": when, "agent": agent, "precision": precision,
                                              "source": "world_advancement:" + advancement_id,
                                              "evidence": _evidence(row)})
            elif relative.startswith("stats/"):
                document = _artifact(run_dir, digest)
                if not isinstance(document, dict):
                    continue
                stats = document.get("stats") if isinstance(document.get("stats"), dict) else {}
                for stage, category, items in STAT_STAGES:
                    values = stats.get(category) if isinstance(stats.get(category), dict) else {}
                    if not any(isinstance(values.get(item), int) and values[item] > 0 for item in items):
                        continue
                    when = origin if baseline else observed
                    if when is None:
                        continue
                    candidates[stage].append({"t": when, "agent": agent,
                                              "precision": "baseline" if baseline else "snapshot_observation_upper_bound",
                                              "source": "world_statistics:" + category, "evidence": _evidence(row)})
        if baseline:
            baseline_seen = True
    return candidates


def _stage_payload(candidates, chats_advancements, origin, duration, running):
    stages = []
    for key, label, dimension in STAGE_ORDER:
        items = list(candidates.get(key) or [])
        # Log advancement titles are precise; snapshot candidates are bounds.
        for item in chats_advancements.get(key, []):
            items.append(item)
        if not items:
            state = "unavailable" if key == "ten_crystals" else ("pending" if running else "not_reached")
            note = ("The paper reads crystals from the players' programs; this harness does not collect "
                    "crystal state yet.") if key == "ten_crystals" else None
            stages.append({"key": key, "label": label, "dimension": dimension, "state": state,
                           "t": None, "agent": None, "precision": None, "source": None, "note": note,
                           "evidence": None})
            continue
        items.sort(key=lambda item: (item["t"], 0 if item.get("precision") != "snapshot_observation_upper_bound" else 1))
        best = items[0]
        t = _clamp(best["t"], origin)
        state = "reached"
        stages.append({"key": key, "label": label, "dimension": dimension, "state": state,
                       "t": t, "agent": best.get("agent"), "precision": best.get("precision"),
                       "source": best.get("source"), "note": None, "evidence": best.get("evidence")})
    return stages


def _behavior_counts(chats):
    counts = Counter()
    by_agent = defaultdict(Counter)
    for item in chats:
        text = (item.get("text") or "").lower()
        for name, pattern in PAPER_BEHAVIORS:
            if re.search(pattern, text):
                counts[name] += 1
                by_agent[item.get("agent") or "unknown"][name] += 1
        for label in item.get("labels") or []:
            name = PROJECT_LABEL_TO_PAPER.get(label)
            if name:
                counts[name + "_via_label"] += 1
    return {"paper": dict(counts), "by_agent": {agent: dict(counter) for agent, counter in by_agent.items()},
            "project_labels": dict(Counter(label for item in chats for label in (item.get("labels") or []))),
            "method": "deterministic regular expressions for paper-style behaviors; project lexical labels are mapped separately; no intent or causal inference"}


def _deaths_payload(deaths):
    raw = Counter(item.get("cause") or "unknown" for item in deaths)
    grouped = Counter()
    by_agent = defaultdict(Counter)
    for cause, count in raw.items():
        group = next((name for name, members in PAPER_DEATH_GROUPS.items() if cause in members), "Other")
        grouped[group] += count
        for item in deaths:
            if (item.get("cause") or "unknown") == cause:
                by_agent[item.get("agent") or "unknown"][group] += 1
    return {"total": len(deaths), "causes": dict(grouped), "raw_causes": dict(raw),
            "by_agent": {agent: dict(counter) for agent, counter in by_agent.items()},
            "method": "paper Figure 13 cause groups; raw server-log causes retained separately"}


def _in_game_days(conn, run_dir):
    row = conn.execute("SELECT event_seq, payload_json FROM events WHERE kind='server_world_snapshot' ORDER BY event_seq DESC LIMIT 1").fetchone()
    if not row:
        return {"value": None, "method": "no registered world snapshot; server level.dat not collected"}
    payload = _json(row["payload_json"]) or {}
    snapshot = _artifact(run_dir, payload.get("artifact_sha256"))
    level = snapshot.get("level") if isinstance(snapshot, dict) else None
    if not isinstance(level, dict) or level.get("error"):
        return {"value": None, "method": "latest server save has no readable level.dat clock"}
    ticks = level.get("time")
    if not isinstance(ticks, int):
        ticks = level.get("daytime")
    day = level.get("day")
    if isinstance(day, int) and day >= 0:
        value = day
    elif isinstance(ticks, int) and ticks >= 0:
        value = ticks // 24000 + 1
    else:
        return {"value": None, "method": "level.dat has no usable Time/Day"}
    return {"value": value, "day": value, "ticks": ticks,
            "method": "world/level.dat clock at latest server save (not agent-reported)",
            "snapshot_event_seq": row["event_seq"]}


def _server_crystal_series(connection, run_dir, origin):
    out = []
    for row in connection.execute("SELECT event_seq, received_at, payload_json FROM events WHERE kind='server_world_snapshot' ORDER BY event_seq"):
        payload = _json(row["payload_json"]) or {}
        document = _artifact(run_dir, payload.get("artifact_sha256"))
        if not isinstance(document, dict):
            continue
        summary = document.get("end_crystals")
        if not isinstance(summary, dict) or summary.get("available") is not True:
            continue
        count = summary.get("count")
        if type(count) is not int or not 0 <= count <= 10:
            continue
        when = _ts(row["received_at"])
        out.append({"t": _clamp(when, origin), "count": count,
                    "positions": summary.get("positions") or [],
                    "event_seq": row["event_seq"], "source": "server save"})
    return out


def _dragon_payload(connection, origin, run_dir):
    """Parse structured observation JSON lines emitted by player programs.

    This is deliberately observation-only: values are agent-observed, never
    server truth, and the UI labels them as such. No extra game tool is exposed;
    agents use the normal console.log path.
    """
    health, crystals = [], []
    server_crystals = _server_crystal_series(connection, run_dir, origin)
    for row in connection.execute("SELECT event_seq, actor_id, received_at, payload_json FROM events WHERE kind='program_log' ORDER BY event_seq DESC LIMIT 5000"):
        payload = _json(row["payload_json"]) or {}
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, str):
            continue
        match = re.search(r"\{[^{}]{0,2000}\}", message)
        if not match:
            continue
        try:
            value = json.loads(match.group(0))
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        stamp = _ts(row["received_at"])
        base = {"agent": row["actor_id"], "event_seq": row["event_seq"], "data": value}
        hp = value.get("dragon_health", value.get("dragonHealth"))
        if isinstance(hp, (int, float)) and not isinstance(hp, bool) and 0 <= hp <= 1000:
            health.append({"t": _clamp(stamp, origin), "hp": float(hp), **base})
        count = value.get("crystals", value.get("crystal_count", value.get("crystalCount")))
        if isinstance(count, (int, float)) and not isinstance(count, bool) and 0 <= count <= 10:
            crystals.append({"t": _clamp(stamp, origin), "count": int(count), **base})
    health.sort(key=lambda item: (item["t"] or 0, item["event_seq"]))
    crystals.sort(key=lambda item: (item["t"] or 0, item["event_seq"]))
    available = bool(health or crystals or server_crystals)
    return {"available": available, "health": health[-MAX_SERIES:], "crystals": crystals[-MAX_SERIES:],
            "server_crystals": server_crystals[-MAX_SERIES:],
            "reason": None if available else "no server crystal snapshot and no player dragon_health/crystals observation",
            "basis": "server save counts End Crystal entities; health and per-crystal attribution are player observations, not server truth"}


def _state(conn):
    state = {}
    try:
        for row in conn.execute("SELECT key, value_json FROM state WHERE key IN ('status','evaluation','active_wall_seconds','world_epoch')"):
            try:
                state[row[0]] = json.loads(row[1])
            except (ValueError, TypeError):
                continue
    except sqlite3.Error:
        return {}
    return state


def _world_payload(manifest, run_dir):
    config = manifest.get("config", {}) if isinstance(manifest, dict) else {}
    server = config.get("server", {}) if isinstance(config, dict) else {}
    world_dir = server.get("world_dir")
    available = bool(world_dir)
    run_id = Path(run_dir).name
    return {"available": available, "url": "/api/world-image?run=" + quote(run_id) + "&style=mesh" if available else None,
            "style": "mesh", "method": "read-only 160x160-block textured 3D mesh around spawn/base; perspective camera, per-pixel z-buffer; complete saved-world LOD mode is also available",
            "caption": "Saved-world 160x160 spawn/base textured 3D mesh from the server save (not agent-reported)."}


def build_figures(run_dir, *, max_series=MAX_SERIES):
    run_dir = Path(run_dir)
    manifest = _json((run_dir / "manifest.json").read_text()) if (run_dir / "manifest.json").is_file() else None
    manifest = manifest if isinstance(manifest, dict) else {}
    config = manifest.get("config") if isinstance(manifest.get("config"), dict) else {}
    agents_cfg = config.get("agents") if isinstance(config.get("agents"), dict) else {}
    agents = [agent for agent in (agents_cfg.get("ids") or []) if isinstance(agent, str)] or ["player1"]
    experiment = config.get("experiment") if isinstance(config.get("experiment"), dict) else {}
    database = run_dir / "events.sqlite3"
    if not database.is_file():
        raise ValueError("events ledger unavailable")
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        state = _state(connection)
        first_row = connection.execute("SELECT event_seq, received_at FROM events ORDER BY event_seq LIMIT 1").fetchone()
        if first_row is None:
            raise ValueError("event ledger empty")
        first_event = connection.execute(
            "SELECT event_seq, received_at FROM events WHERE kind IN ('execution_running','execution_accepted','execution_submitted') ORDER BY event_seq LIMIT 1").fetchone()
        origin_row = first_event or connection.execute(
            "SELECT event_seq, received_at FROM events WHERE kind IN ('run_started','experiment_started') ORDER BY event_seq LIMIT 1").fetchone() or first_row
        origin = _ts(origin_row["received_at"])
        last_row = connection.execute("SELECT event_seq, received_at FROM events ORDER BY event_seq DESC LIMIT 1").fetchone()
        end = _ts(last_row["received_at"]) if last_row else origin
        if origin is None or end is None:
            raise ValueError("ledger times unavailable")
        duration = max(0.0, end - origin)
        run_status = state.get("status") if isinstance(state.get("status"), str) else "unknown"
        running = run_status in {"running", "starting", "recovering"}
        programs = _program_series(connection, origin)
        jobs = _job_series(connection, origin)
        chats, deaths, advancements, omitted = _server_items(connection, run_dir)
        candidates = _snapshot_stages(connection, run_dir, agents, origin)
        precise = defaultdict(list)
        for item in advancements:
            stage = TITLE_STAGE.get((item.get("title") or "").strip().lower())
            if stage:
                precise[stage].append({"t": item["t"], "agent": item.get("agent"), "precision": "source_time",
                                       "source": "server_log:" + str(item.get("title")), "evidence": item.get("evidence")})
        dragon = _dragon_payload(connection, origin, run_dir)
        zero_crystals = [item for item in dragon.get("crystals", []) if item.get("count") == 0]
        if zero_crystals:
            first = min(zero_crystals, key=lambda item: (item.get("t") or 0, item.get("event_seq") or 0))
            candidates["ten_crystals"].append({"t": first["t"], "agent": first.get("agent"),
                "precision": "player_program_observation", "source": "program_observation:ten_crystals",
                "evidence": {"event_seq": first.get("event_seq")}})
        server_zero = [item for item in dragon.get("server_crystals", []) if item.get("count") == 0]
        if server_zero:
            first = min(server_zero, key=lambda item: (item.get("t") or 0, item.get("event_seq") or 0))
            candidates["ten_crystals"].append({"t": first["t"], "agent": None,
                "precision": "snapshot_observation_upper_bound", "source": "server_save:end_crystals",
                "evidence": {"event_seq": first.get("event_seq")}})
        stages = _stage_payload(candidates, precise, origin, duration, running)
        for item in chats + deaths + advancements:
            item["t"] = _clamp(item.get("t"), origin)
        chats.sort(key=lambda item: item["t"] or 0)
        deaths.sort(key=lambda item: item["t"] or 0)
        advancements.sort(key=lambda item: item["t"] or 0)
        # Bound the payload; omitted counts stay visible.
        def bound(items):
            kept = items[:max_series]
            return kept, max(0, len(items) - len(kept))
        chats, chats_omitted = bound(chats)
        deaths, deaths_omitted = bound(deaths)
        advancements, advancements_omitted = bound(advancements)
        programs, programs_omitted = bound(programs)
        jobs, jobs_omitted = bound(jobs)
        reached = [stage for stage in stages if stage["state"] == "reached"]
        dimension_bands = []
        labels = {"overworld": "Overworld", "nether": "Nether", "stronghold": "Stronghold", "end": "The End"}
        points = {stage["key"]: stage["t"] for stage in reached}
        boundaries = [("overworld", 0.0)]
        if points.get("nether_portal") is not None:
            boundaries.append(("nether", points["nether_portal"]))
        if points.get("stronghold") is not None:
            boundaries.append(("stronghold", points["stronghold"]))
        if points.get("the_end") is not None:
            boundaries.append(("end", points["the_end"]))
        boundaries.sort(key=lambda item: item[1])
        for index, (key, start) in enumerate(boundaries):
            finish = boundaries[index + 1][1] if index + 1 < len(boundaries) else duration
            if finish >= start:
                dimension_bands.append({"key": key, "label": labels[key], "start": round(start, 3), "end": round(max(start, finish), 3)})
        agent_cards = []
        for index, agent in enumerate(agents):
            agent_cards.append({"id": agent, "color": AGENT_COLORS[index % len(AGENT_COLORS)],
                                "programs": sum(1 for item in programs if item["agent"] == agent),
                                "messages": sum(1 for item in chats if item["agent"] == agent),
                                "deaths": sum(1 for item in deaths if item["agent"] == agent),
                                "advancements": sum(1 for item in advancements if item["agent"] == agent)})
        # Paper Figure 3 orange marks are *world-server restarts*, not worker
        # runtime resets. Keep the two phenomena separate so a 16-reset soak
        # run cannot visually masquerade as 16 paper-style server restarts.
        restarts = []
        runtime_resets = []
        for row in connection.execute("SELECT event_seq, kind, actor_id, received_at, payload_json FROM events WHERE kind IN ('server_stopped','server_started','world_restored','runtime_recovery_started','runtime_reset') ORDER BY event_seq"):
            payload = _json(row["payload_json"]) or {}
            when = _ts(row["received_at"])
            if when is None:
                continue
            item = {"t": _clamp(when, origin), "kind": row["kind"], "reason": (payload.get("reason") if isinstance(payload, dict) else None),
                    "event_seq": row["event_seq"]}
            if row["kind"] in {'server_stopped', 'server_started', 'world_restored'}:
                restarts.append(item)
            else:
                runtime_resets.append(item)
        evaluation = state.get("evaluation") if isinstance(state.get("evaluation"), dict) else {}
        payload = {
            "schema_version": SCHEMA_VERSION,
            "run": {"id": manifest.get("run_id") or run_dir.name, "dir": run_dir.name,
                    "name": experiment.get("name"), "task": experiment.get("task"),
                    "profile": experiment.get("profile"), "status": run_status,
                    "running": running, "origin": origin_row["received_at"],
                    "end": last_row["received_at"], "duration_s": round(duration, 3),
                    "event_count": connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                    "capability_success": evaluation.get("capability_success"),
                    "evaluation_status": evaluation.get("status")},
            "agents": agent_cards, "programs": programs, "jobs": jobs,
            "chats": chats, "deaths": deaths, "advancements": advancements,
            "stages": stages, "dimension_bands": dimension_bands,
            "restarts": restarts, "runtime_resets": runtime_resets,
            "behavior": _behavior_counts(chats), "death_causes": _deaths_payload(deaths),
            "dragon": dragon, "in_game_days": _in_game_days(connection, run_dir),
            "world": _world_payload(manifest, run_dir),
            "omitted": {"programs": programs_omitted, "jobs": jobs_omitted, "chats": chats_omitted,
                        "deaths": deaths_omitted, "advancements": advancements_omitted, **dict(omitted)},
            "method": {
                "origin": "first program start (accepted/running event), or run_started when no program exists",
                "time": "seconds since the first program",
                "stages": "advancement/stat evidence from registered world snapshots and normalized server log; snapshot observations are upper bounds",
                "authority": "server files and normalized server log only; agent claims are excluded from success and milestones",
                "figures": "Fig.1/4 stages and death strip; Fig.3 lanes and program bars; Fig.5/6/8/14 chat explorer; Fig.12 player-observed crystals/health; Fig.13 paper behavior/death groups",
                "journal": "Fig.2-style saved-world top-down block map is available when server world files are readable; no true 3D mesh/texture renderer",
                "in_game_days": "computed from level.dat Time/Day in the latest registered server save",
            },
            "notes": ["The paper's ten-crystal step has no advancement/statistics equivalent and remains unavailable here.",
                      "Chat behavior categories are deterministic approximations, not semantic intent.",
                      "Stage times from snapshots are upper bounds; hover shows the precision and evidence reference."],
        }
        return payload
    finally:
        connection.close()

_CACHE = {}
_CACHE_TIME = {}
_CACHE_TTL_S = 30.0
_CACHE_LOCK = threading.RLock()


def figures_for_dashboard(run_dir):
    """Return the figure payload, rebuilding only when the ledger advanced."""
    run_dir = Path(run_dir)
    database = run_dir / "events.sqlite3"
    if not database.is_file():
        return {"error": "events ledger unavailable"}
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        maximum = connection.execute("SELECT MAX(event_seq) FROM events").fetchone()[0]
        row = connection.execute("SELECT value_json FROM state WHERE key='status'").fetchone()
        status = row[0] if row else None
    finally:
        connection.close()
    key = (str(run_dir), maximum, status)
    with _CACHE_LOCK:
        now = time.monotonic()
        cached = _CACHE.get(str(run_dir))
        # TTL, not event_seq equality: a live ledger advances every model call;
        # rebuilding the entire figure projection on each poll made the gallery
        # O(history) per 15s. The page may lag by at most the TTL.
        if cached and now - _CACHE_TIME.get(str(run_dir), 0.0) < _CACHE_TTL_S:
            return cached[1]
        payload = build_figures(run_dir)
        _CACHE[str(run_dir)] = (key, payload)
        _CACHE_TIME[str(run_dir)] = now
        while len(_CACHE) > 8:
            old = next(iter(_CACHE))
            _CACHE.pop(old, None); _CACHE_TIME.pop(old, None)
        return payload
