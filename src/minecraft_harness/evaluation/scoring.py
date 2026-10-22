"""Versioned rules score server files, never player stdout or model opinions."""

from __future__ import annotations

import json
import copy
from collections import OrderedDict
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from .collector import artifact_bytes, registered_evidence, iter_registered_evidence

SUPPORTED_TASKS = ("observe_only", "obtain_logs", "craft_table", "obtain_iron_ingot", "kill_dragon")
CRITERION_VERSION = "minecraft-java-1.20.4-server-files-v1"
LOG_ITEMS = {"minecraft:" + name for name in (
    "oak_log", "spruce_log", "birch_log", "jungle_log", "acacia_log", "dark_oak_log",
    "mangrove_log", "cherry_log", "crimson_stem", "warped_stem",
)}


def _event_payload(event: dict) -> dict:
    payload = event.get("payload", {})
    if isinstance(payload, dict):
        return payload
    return {}


def _configuration(task_config: dict | str) -> tuple[str, int, set[str] | None]:
    if isinstance(task_config, str):
        return task_config, 1, None
    if not isinstance(task_config, dict):
        raise ValueError("task_config must be a task name or object")
    extra = set(task_config) - {"name", "id", "task", "min_count", "count", "player_ids"}
    if extra:
        raise ValueError("Unsupported task criteria: " + ", ".join(sorted(extra)))
    name = task_config.get("name", task_config.get("id", task_config.get("task", "")))
    minimum = task_config.get("min_count", task_config.get("count", 1))
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        raise ValueError("min_count must be a positive integer")
    if name in ("observe_only", "kill_dragon") and minimum != 1:
        raise ValueError("This task supports only min_count=1")
    players = task_config.get("player_ids")
    if players is not None and (not isinstance(players, list) or not players or any(not isinstance(p, str) for p in players)):
        raise ValueError("player_ids must be a nonempty list")
    return str(name), minimum, None if players is None else {p.lower() for p in players}


def _stats(files: dict, category: str, items: set[str], players: set[str] | None) -> int:
    total = 0
    for relative, document in files.items():
        if not relative.startswith("stats/") or (players is not None and Path(relative).stem not in players):
            continue
        values = document.get("stats", {}).get(category, {})
        if not isinstance(values, dict):
            raise ValueError("Malformed statistics category")
        for item in items:
            value = values.get(item, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Malformed server statistic")
            total += value
    return total


def _advancement(files: dict, name: str, players: set[str] | None) -> set[str]:
    result = set()
    for relative, document in files.items():
        if not relative.startswith("advancements/") or (players is not None and Path(relative).stem not in players):
            continue
        entry = document.get(name, {})
        if isinstance(entry, dict) and entry.get("done") is True:
            result.add(Path(relative).stem)
    return result


def _server_log_success(run_dir: Path) -> dict | None:
    """Paper §5 authority: the server log's [Free the End] advancement line."""
    for row in iter_registered_evidence(run_dir):
        if row.get("kind") != "server_log_segment":
            continue
        try:
            document = json.loads(artifact_bytes(run_dir, row["artifact_sha256"]))
        except (OSError, ValueError, UnicodeError):
            continue
        for item in document.get("events", []) if isinstance(document, dict) else []:
            message = item.get("raw_message") if isinstance(item, dict) else None
            if isinstance(message, str) and "free the end" in message.lower():
                return {"event_seq": row.get("event_seq"), "evidence_id": row.get("evidence_id"),
                        "artifact_sha256": row.get("artifact_sha256"), "source_time": item.get("source_time"),
                        "raw_message": message[:1000], "actor_id": item.get("actor_id"),
                        "method": "server log [Free the End] line"}
    return None


def _load_snapshot(run_dir: Path, row: dict) -> dict:
    document = json.loads(artifact_bytes(run_dir, row["artifact_sha256"]))
    if document.get("kind") != "world_snapshot" or not isinstance(document.get("files"), dict):
        raise ValueError("Invalid registered world snapshot")
    files = {}
    refs = []
    for relative, digest in sorted(document["files"].items()):
        if not (relative.startswith("stats/") or relative.startswith("advancements/")):
            raise ValueError("Invalid server file type")
        files[relative] = json.loads(artifact_bytes(run_dir, digest))
        if not isinstance(files[relative], dict):
            raise ValueError("Invalid server JSON document")
        refs.append({"artifact_sha256": digest, "relative_path": relative})
    return {**row, "files": files, "scope": document.get("scope"), "player_ids": document.get("player_ids"), "file_refs": refs}


def _reference(snapshot: dict) -> dict:
    return {"event_seq": snapshot["event_seq"], "evidence_id": snapshot["evidence_id"],
            "artifact_sha256": snapshot["artifact_sha256"], "world_epoch": snapshot["world_epoch"],
            "files": snapshot["file_refs"]}


def _lineage_windows(events: Iterable[dict], epoch: int) -> dict[int, int | None]:
    """Map the surviving world history to inclusive ledger cutoffs.

    A checkpoint restore discards its abandoned branch, while carrying the
    experiment's original baseline and achievements actually in the checkpoint.
    """
    transitions = {}
    for event in events:
        if event.get("kind") == "world_restored" and event.get("source") == "harness":
            transitions[int(event.get("world_epoch", 0))] = event
    windows = {}
    bound = None
    while epoch >= 0 and epoch not in windows:
        windows[epoch] = bound
        transition = transitions.get(epoch)
        if transition is None or (bound is not None and transition.get("event_seq", 0) > bound):
            break
        payload = _event_payload(transition)
        if payload.get("rollback") is False:
            source = payload.get("source_world_epoch", epoch - 1)
        elif payload.get("rollback") is True:
            source, cutoff = payload.get("source_world_epoch"), payload.get("restore_point_event_seq")
            if type(source) is not int or type(cutoff) is not int or cutoff < 0:
                break
            bound = min(bound, cutoff) if bound is not None else cutoff
        else:
            break
        if type(source) is not int or source < 0 or source >= epoch:
            break
        epoch = source
    return windows


def evaluate(
    run_dir: str | Path,
    task_config: dict | str,
    events: Iterable[dict] | None = None,
    *,
    _rows: list[dict] | None = None,
    _snapshot_cache: OrderedDict | None = None,
    _frontier: bool = False,
    _active_epochs: set[int] | None = None,
    _windows: dict[int, int | None] | None = None,
) -> dict:
    """Return deterministic scores and evidence references for one run.

    ``events`` limits scoring to evidence actually present in the trusted recorder
    projection. Registered artifacts are rehashed and parsed independently. An
    unregistered event, arbitrary ``source`` label, claimed inventory or recipe
    unlock can never satisfy a criterion. Without a trusted pre-action baseline,
    a pre-existing achievement cannot be attributed to this experiment.
    """
    run_dir = Path(run_dir).resolve()
    if events is None:
        database = run_dir / "events.sqlite3"
        event_list = []
        if database.is_file():
            connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            try:
                connection.row_factory = sqlite3.Row
                task_name = task_config if isinstance(task_config, str) else task_config.get("name", task_config.get("id", task_config.get("task"))) if isinstance(task_config, dict) else None
                if task_name == "observe_only":
                    query = "SELECT * FROM events WHERE source IN ('harness','runtime','bot_runtime') AND (kind IN ('execution_completed','program_succeeded','execution_succeeded') OR (kind IN ('execution_finished','program_finished') AND json_extract(payload_json,'$.status')='succeeded')) ORDER BY event_seq LIMIT 1"
                else:
                    query = "SELECT * FROM events WHERE kind IN ('server_world_snapshot','world_restored','world_recovered','world_checkpoint_restored') ORDER BY event_seq"
                for row in connection.execute(query):
                    event = dict(row)
                    event["payload"] = json.loads(event.pop("payload_json"))
                    event_list.append(event)
                latest_epoch = connection.execute("SELECT MAX(world_epoch) FROM events WHERE source!='agent_claim'").fetchone()[0]
                if latest_epoch is not None:
                    event_list.append({"event_seq": -1, "world_epoch": latest_epoch, "kind": "scoring_epoch", "source": "harness", "payload": {}})
            finally:
                connection.close()
    else:
        event_list = list(events)
    result: dict[str, Any] = {
        "schema_version": 1, "criterion_version": CRITERION_VERSION,
        "task": task_config if isinstance(task_config, str) else (task_config.get("name", task_config.get("id", task_config.get("task"))) if isinstance(task_config, dict) else None),
        "status": "not_succeeded", "capability_success": False, "world_epoch": 0,
        "criteria": [], "evidence_refs": [], "warnings": [], "first_success_event_seq": None,
    }
    try:
        name, minimum, players = _configuration(task_config)
    except (ValueError, TypeError) as error:
        result.update(status="unsupported", capability_success=None, reason=str(error))
        return result
    result["task"] = name
    if name not in SUPPORTED_TASKS:
        result.update(status="unsupported", capability_success=None, reason="No independently verifiable rule for this task")
        return result
    rows = registered_evidence(run_dir) if _rows is None else _rows
    if event_list is not None:
        event_index = {event.get("event_seq"): event for event in event_list}
        rows = [row for row in rows if (event := event_index.get(row["event_seq"])) is not None
                and event.get("kind") == row["kind"]
                and event.get("actor_id") == "server_collector"
                and event.get("source") == "server"
                and int(event.get("world_epoch", -1)) == row["world_epoch"]
                and _event_payload(event).get("evidence_id") == row["evidence_id"]
                and _event_payload(event).get("artifact_sha256") == row["artifact_sha256"]]
    epoch = max([int(row["world_epoch"]) for row in rows] + [int(event.get("world_epoch", 0)) for event in (event_list or [])] + [0])
    result["world_epoch"] = epoch
    windows = _windows or _lineage_windows(event_list, epoch)
    if name == "observe_only":
        passed = any((event.get("kind") in ("execution_completed", "program_succeeded", "execution_succeeded")
                     or (event.get("kind") in ("execution_finished", "program_finished") and _event_payload(event).get("status") == "succeeded"))
                     and event.get("source") in ("harness", "runtime", "bot_runtime")
                     for event in (event_list or []))
        result.update(status="diagnostic_pass" if passed else "insufficient_evidence", capability_success=None,
                      reason="Connectivity diagnostic only; no Minecraft capability claim")
        return result
    def snapshots():
        for row in rows:
            cutoff = windows.get(row["world_epoch"])
            if row["kind"] != "server_world_snapshot" or row["world_epoch"] not in windows or (cutoff is not None and row["event_seq"] > cutoff):
                continue
            try:
                key = (row["evidence_id"], row["artifact_sha256"])
                snapshot = _snapshot_cache.get(key) if _snapshot_cache is not None else None
                if snapshot is None:
                    snapshot = _load_snapshot(run_dir, row)
                    if _snapshot_cache is not None:
                        _snapshot_cache[key] = snapshot
                        while len(_snapshot_cache) > 8:
                            _snapshot_cache.popitem(last=False)
                yield snapshot
            except (ValueError, OSError, TypeError, KeyError) as error:
                if len(result["warnings"]) < 100:
                    result["warnings"].append({"event_seq": row["event_seq"], "error": str(error)})
                else:
                    result["warnings_omitted"] = result.get("warnings_omitted", 0) + 1
    iterator = iter(snapshots())
    baseline = next((snapshot for snapshot in iterator if snapshot["baseline"]), None)
    if baseline is None:
        result.update(status="insufficient_evidence", reason="No valid trusted baseline in current world epoch")
        return result
    # A save/checkpoint/restart within the same world lineage must not erase
    # accumulated progress. Only a real rollback establishes a new epoch.
    if players is not None and baseline["scope"] == "explicit_players" and not players.issubset(set(baseline["player_ids"] or [])):
        result.update(status="insufficient_evidence", reason="Baseline does not cover requested players")
        return result
    if players is None and baseline["scope"] == "explicit_players":
        players = set(baseline["player_ids"] or [])
    before = baseline["files"]
    best = 0
    successful = None
    best_snapshot = latest_snapshot = None
    for snapshot in iterator:
        latest_snapshot = snapshot
        if best_snapshot is None:
            best_snapshot = snapshot
        # Restrict both snapshots to the same player scope. Excluded baseline
        # players must not silently reappear with a zero historical counter.
        if players is not None and snapshot["scope"] == "explicit_players" and not players.issubset(set(snapshot["player_ids"] or [])):
            if len(result["warnings"]) < 100:
                result["warnings"].append({"event_seq": snapshot["event_seq"], "error": "Snapshot does not cover requested players"})
            continue
        after = snapshot["files"]
        try:
            if name == "obtain_logs":
                value = max(0, _stats(after, "minecraft:picked_up", LOG_ITEMS, players) - _stats(before, "minecraft:picked_up", LOG_ITEMS, players))
                method = "picked_up_log_item_delta"
            elif name == "craft_table":
                value = max(0, _stats(after, "minecraft:crafted", {"minecraft:crafting_table"}, players) - _stats(before, "minecraft:crafted", {"minecraft:crafting_table"}, players))
                method = "crafted_table_delta"
            elif name == "obtain_iron_ingot":
                picked = max(0, _stats(after, "minecraft:picked_up", {"minecraft:iron_ingot"}, players) - _stats(before, "minecraft:picked_up", {"minecraft:iron_ingot"}, players))
                crafted = max(0, _stats(after, "minecraft:crafted", {"minecraft:iron_ingot"}, players) - _stats(before, "minecraft:crafted", {"minecraft:iron_ingot"}, players))
                gained = len(_advancement(after, "minecraft:story/smelt_iron", players) - _advancement(before, "minecraft:story/smelt_iron", players))
                # These can describe the same acquisition; never double-count.
                value = max(picked, crafted, gained if minimum == 1 else 0)
                method = "iron_acquisition_delta_or_new_smelt_iron_advancement"
            else:
                value = int(bool(_advancement(after, "minecraft:end/kill_dragon", players) - _advancement(before, "minecraft:end/kill_dragon", players)))
                method = "new_kill_dragon_advancement"
        except (ValueError, TypeError, AttributeError) as error:
            if len(result["warnings"]) < 100:
                result["warnings"].append({"event_seq": snapshot["event_seq"], "error": str(error)})
            continue
        if value > best:
            best, best_snapshot = value, snapshot
        if value >= minimum and successful is None:
            successful = snapshot
    if latest_snapshot is None:
        result.update(status="insufficient_evidence", reason="No valid post-baseline server snapshot")
        return result
    log_success = _server_log_success(run_dir) if name == "kill_dragon" else None
    if name == "kill_dragon" and log_success:
        result.update(status="succeeded", capability_success=True,
                      first_success_event_seq=log_success["event_seq"],
                      success_authority="server_log_free_the_end", success_log=log_success)
        result["criteria"] = [{"name": name, "method": "server_log_free_the_end",
                               "observed_delta": 1, "required_delta": 1, "passed": True}]
        result["evidence_refs"] = [{key: log_success.get(key) for key in
                                    ("event_seq", "evidence_id", "artifact_sha256", "source_time", "raw_message")}]
        return result
    if name == "kill_dragon" and successful is not None:
        # A world-file advancement alone is no longer sufficient: the paper's
        # authoritative win record is the server's [Free the End] log line.
        result.update(status="insufficient_evidence", capability_success=False,
                      reason="advancement found but authoritative [Free the End] server-log line is missing")
        result.setdefault("warnings", []).append({"error": "success_log_required",
                                                  "event_seq": successful["event_seq"]})
        return result
    chosen = successful or best_snapshot
    if _frontier:
        result["_frontier"] = sorted({baseline["event_seq"], best_snapshot["event_seq"], chosen["event_seq"], latest_snapshot["event_seq"]})
    result["criteria"] = [{"name": name, "method": method if 'method' in locals() else "server_files",
                           "observed_delta": best, "required_delta": minimum, "passed": successful is not None}]
    result["evidence_refs"] = [_reference(baseline), _reference(chosen)]
    if successful is not None:
        result.update(status="succeeded", capability_success=True, first_success_event_seq=successful["event_seq"])
    elif result["warnings"] and best == 0:
        result.update(status="insufficient_evidence", reason="Server snapshots were incomplete or invalid")
    else:
        result["reason"] = "Verified server delta has not met the task criterion"
    return result


class IncrementalEvaluator:
    """Bounded live scorer; forensic ``evaluate`` always rehashes from disk.

    Keep one instance per supervisor. Normal polls query only ledger rows after
    the prior cursor and retain at most four snapshot references/eight parsed
    snapshots. Work grows with new evidence, rather than the experiment's age.
    Artifacts must remain operator-owned and immutable while live scoring uses
    its cache; final reports should use a fresh forensic evaluation.
    """
    def __init__(self, run_dir: str | Path, task_config: dict | str):
        self.run_dir = Path(run_dir).resolve()
        self.task_config = copy.deepcopy(task_config)
        self.cursor = 0
        self.epoch = 0
        self.epochs = {0}
        self.windows = {0: None}
        self.transitions = {}
        self.rows: list[dict] = []
        self.events: dict[int, dict] = {}
        self.cache = OrderedDict()
        self.result = None
        self.diagnostic_event = None

    def evaluate(self) -> dict:
        database = self.run_dir / "events.sqlite3"
        batch = []
        diagnostic = None
        if database.is_file():
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                query = """SELECT event_seq,world_epoch,kind,source,actor_id,
                    CASE WHEN kind IN ('server_world_snapshot','world_restored','world_recovered','world_checkpoint_restored','server_log_segment','execution_completed','program_succeeded','execution_succeeded','execution_finished','program_finished')
                    THEN payload_json ELSE '{}' END AS payload_json
                    FROM events WHERE event_seq>? ORDER BY event_seq"""
                for row in connection.execute(query, (self.cursor,)):
                    # Filter before parsing large program/tool payloads.
                    self.cursor = row["event_seq"]
                    self.epoch = max(self.epoch, row["world_epoch"])
                    if row["kind"] not in {"server_world_snapshot", "world_restored", "world_recovered", "world_checkpoint_restored", "server_log_segment", "execution_completed", "program_succeeded", "execution_succeeded", "execution_finished", "program_finished"}:
                        continue
                    if row["kind"] in {"execution_completed", "program_succeeded", "execution_succeeded", "execution_finished", "program_finished"} and (self.diagnostic_event is not None or diagnostic is not None):
                        continue
                    event = dict(row)
                    event["payload"] = json.loads(event.pop("payload_json"))
                    if event["kind"] in {"execution_completed", "program_succeeded", "execution_succeeded", "execution_finished", "program_finished"}:
                        if self.diagnostic_event is None and diagnostic is None and event.get("source") in {"harness", "runtime", "bot_runtime"} and (event["kind"] in {"execution_completed", "program_succeeded", "execution_succeeded"} or _event_payload(event).get("status") == "succeeded"):
                            diagnostic = event
                        continue
                    batch.append(event)
        if diagnostic:
            batch.append(diagnostic)
        for event in batch:
            if event["kind"] == "world_restored" and event.get("source") == "harness":
                self.transitions[event["world_epoch"]] = event
        windows = _lineage_windows(self.transitions.values(), self.epoch)
        continuous = all(epoch in windows and windows[epoch] == cutoff for epoch, cutoff in self.windows.items())
        replay = windows != self.windows and not continuous
        if windows != self.windows:
            self.windows, self.epochs = windows, set(windows)
            self.result = None
        if replay:
            self.rows, self.events = [], {}
            self.cache.clear()
        new_rows = registered_evidence(self.run_dir, after=min((event["event_seq"] for event in batch), default=self.cursor + 1) - 1) if batch else []
        if replay:
            new_rows = registered_evidence(self.run_dir)
            sequences = [row["event_seq"] for row in new_rows if row["world_epoch"] in self.windows]
            if sequences:
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                    connection.row_factory = sqlite3.Row
                    # Batches avoid SQLite's bound-parameter limit.
                    for offset in range(0, len(sequences), 256):
                        selected = sequences[offset:offset + 256]
                        for row in connection.execute("SELECT * FROM events WHERE event_seq IN (" + ",".join("?" for _ in selected) + ")", selected):
                            event = dict(row)
                            event["payload"] = json.loads(event.pop("payload_json"))
                            self.events[event["event_seq"]] = event
        # A crash may leave evidence committed before ledger linkage; refresh
        # sees it on the next relevant event or a newly constructed scorer.
        for event in batch:
            if event["kind"] == "server_world_snapshot":
                self.events[event["event_seq"]] = event
            elif event["kind"] in {"execution_completed", "program_succeeded", "execution_succeeded", "execution_finished", "program_finished"}:
                if event.get("source") in {"harness", "runtime", "bot_runtime"} and (event["kind"] in {"execution_completed", "program_succeeded", "execution_succeeded"} or _event_payload(event).get("status") == "succeeded"):
                    self.diagnostic_event = event
        if not new_rows and self.result is not None and not batch:
            return copy.deepcopy(self.result)
        known = {existing["event_seq"] for existing in self.rows}
        self.rows.extend(row for row in new_rows if row["kind"] == "server_world_snapshot" and row["world_epoch"] in self.windows and (self.windows[row["world_epoch"]] is None or row["event_seq"] <= self.windows[row["world_epoch"]]) and row["event_seq"] not in known)
        self.rows.sort(key=lambda row: row["event_seq"])
        event_list = list(self.events.values())
        if self.diagnostic_event:
            event_list.append(self.diagnostic_event)
        event_list.append({"event_seq": -1, "world_epoch": self.epoch, "kind": "scoring_epoch", "source": "harness", "payload": {}})
        result = evaluate(self.run_dir, self.task_config, event_list, _rows=self.rows,
                          _snapshot_cache=self.cache, _frontier=True, _windows=self.windows)
        keep = set(result.pop("_frontier", []))
        if not keep:
            # Retain the earliest baseline and latest candidate if no complete
            # criterion exists yet (for example a malformed snapshot).
            baselines = [row for row in self.rows if row["baseline"]]
            keep = {row["event_seq"] for row in baselines[:1] + self.rows[-1:]}
        self.rows = [row for row in self.rows if row["event_seq"] in keep]
        self.events = {seq: event for seq, event in self.events.items() if seq in keep}
        self.result = result
        return copy.deepcopy(result)
