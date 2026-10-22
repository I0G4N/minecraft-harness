"""Deterministic Pi-style context projection for one agent.

The harness keeps an append-only context journal under the agent's state
directory.  It records only *projections* (not the raw conversation, which stays
in the checkpoint / archive artifacts):

* ``compaction`` -- the structured summary that replaced older messages;
* ``recovery`` -- why a branch/recovery summary was emitted after a restart or
  world/runtime reset.

The projection is deliberately model-free and deterministic: file effects are
derived from tool arguments, pending work comes from the live agent state, and
the compaction summary fields follow the categories the paper requires
(objective, work in progress, next steps, decisions, verified/failed facts,
files read/modified, blockers, pending work, epochs).  No Pi product concepts
(session trees, extensions, MCP) are exposed to the model.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

CONTEXT_SCHEMA_VERSION = 1
JOURNAL_FILE_NAME = "context-journal.jsonl"
MAX_TRACKED_FILES = 64
MAX_LIST_ITEMS = 20
MAX_ITEM_CHARS = 500
MAX_DECISIONS = 20
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_RECOVERY_MESSAGE_BYTES = 6000

# The paper's CLI is the only game interface in paper mode, so a shell command
# of the documented form is the one shell pattern we inspect.  Arbitrary shell
# text is intentionally not parsed for file effects.
_MC_EXEC_FILE = re.compile(
    r"\bmc\s+exec\b[^\n;|&]*?--file(?:=|\s+)([^\s;|&'\"]+)", re.IGNORECASE)

_FILE_TOOLS_READ = {"workspace_read"}
_FILE_TOOLS_MODIFIED = {"workspace_write", "workspace_remove"}
_PROGRAM_TOOLS = {"mc_execute", "mc_submit"}


class ContextEngine:
    """Per-agent context projection + journal (no model calls, no randomness)."""

    def __init__(self, state_dir: Path, *, agent_id: str = "", goal: str = "") -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.agent_id = str(agent_id or "")
        self.goal = str(goal or "")
        self.journal_path = self.state_dir / JOURNAL_FILE_NAME
        self.files_read: list[str] = []
        self.files_modified: list[str] = []
        self.decisions: list[str] = []
        self.compactions = 0
        self.recoveries = 0
        self.last_summary: dict[str, Any] | None = None
        self._replay_journal()

    # -- persistence ----------------------------------------------------
    def state(self) -> dict[str, Any]:
        """Serializable projection state stored in the agent checkpoint."""
        return {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "files_read": list(self.files_read),
            "files_modified": list(self.files_modified),
            "decisions": list(self.decisions),
            "compactions": self.compactions,
            "recoveries": self.recoveries,
            "last_summary": self.last_summary,
        }

    def load_state(self, state: Any) -> None:
        """Restore checkpoint projection state; tolerate old/invalid checkpoints."""
        if not isinstance(state, dict):
            return
        if state.get("schema_version") not in {None, CONTEXT_SCHEMA_VERSION}:
            return
        self.files_read = self._merge_files(
            self._strings(state.get("files_read"), MAX_TRACKED_FILES, 512),
            self.files_read)[-MAX_TRACKED_FILES:]
        self.files_modified = self._merge_files(
            self._strings(state.get("files_modified"), MAX_TRACKED_FILES, 512),
            self.files_modified)[-MAX_TRACKED_FILES:]
        self.decisions = self._merge_unique(
            self._strings(state.get("decisions"), MAX_DECISIONS, MAX_ITEM_CHARS),
            self.decisions)[-MAX_DECISIONS:]
        for name in ("compactions", "recoveries"):
            value = state.get(name)
            if type(value) is int and value >= 0:
                setattr(self, name, max(getattr(self, name), value))
        summary = state.get("last_summary")
        if isinstance(summary, dict):
            self.last_summary = self._bounded_mapping(summary)

    def _append_journal(self, entry: dict[str, Any]) -> None:
        """Append one durable JSONL entry (compaction/recovery are infrequent)."""
        record = {**entry, "schema_version": CONTEXT_SCHEMA_VERSION}
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        try:
            with self.journal_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            # The journal is an internal audit trail; an unwritable disk must be
            # surfaced by the agent's own checkpoint writer instead.
            return

    def _replay_journal(self) -> None:
        try:
            if not self.journal_path.exists():
                return
            size = self.journal_path.stat().st_size
            with self.journal_path.open("rb") as handle:
                if size > MAX_JOURNAL_BYTES:
                    handle.seek(-MAX_JOURNAL_BYTES, os.SEEK_END)
                    raw = handle.read()
                    raw = raw.split(b"\n", 1)[-1]
                else:
                    raw = handle.read()
        except OSError:
            return
        entries: list[dict[str, Any]] = []
        for line in raw.splitlines()[-256:]:
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(value, dict):
                entries.append(value)
        for entry in entries:
            kind = entry.get("kind")
            if kind == "compaction":
                summary = entry.get("summary")
                if isinstance(summary, dict):
                    self.last_summary = self._bounded_mapping(summary)
                    self.files_read = self._merge_files(
                        self.files_read,
                        self._strings(summary.get("files_read"), MAX_TRACKED_FILES, 512))[-MAX_TRACKED_FILES:]
                    self.files_modified = self._merge_files(
                        self.files_modified,
                        self._strings(summary.get("files_modified"), MAX_TRACKED_FILES, 512))[-MAX_TRACKED_FILES:]
                    self.decisions = self._merge_unique(
                        self.decisions,
                        self._strings(summary.get("decisions"), MAX_DECISIONS, MAX_ITEM_CHARS))[-MAX_DECISIONS:]
                if type(entry.get("compactions")) is int:
                    self.compactions = max(self.compactions, entry["compactions"])
            elif kind == "recovery" and type(entry.get("recoveries")) is int:
                self.recoveries = max(self.recoveries, entry["recoveries"])

    # -- normalisation helpers -----------------------------------------
    @staticmethod
    def _strings(value: Any, limit: int, max_chars: int) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        result: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            text = " ".join(item.split()).strip()
            if not text:
                continue
            result.append(text[:max_chars])
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def _merge_unique(first: Iterable[str], second: Iterable[str]) -> list[str]:
        result: list[str] = []
        for item in [*first, *second]:
            if item in result:
                result.remove(item)
            result.append(item)
        return result

    def _merge_files(self, first: Iterable[str], second: Iterable[str]) -> list[str]:
        merged = self._merge_unique(first, second)
        return merged[-MAX_TRACKED_FILES:]

    @staticmethod
    def _bounded_mapping(value: Any, max_bytes: int = 8000) -> dict[str, Any]:
        try:
            encoded = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
        except (TypeError, ValueError):
            return {}
        if len(encoded.encode("utf-8")) <= max_bytes:
            return json.loads(encoded)
        return {"truncated": True, "excerpt": encoded[:max_bytes]}

    @staticmethod
    def _bounded_details(details: Any) -> Any:
        if details is None:
            return None
        return ContextEngine._bounded_mapping(details, 4000)

    # -- tool file-effect tracking -------------------------------------
    def track_tool(self, tool: str, arguments: Any, result: Any = None) -> None:
        """Record file effects of one successfully dispatched tool call.

        Only workspace tools and the documented ``mc exec --file <path>`` shell
        form are inspected; arbitrary shell text is never parsed.  Tracking may
        never raise into the agent loop.
        """
        try:
            if not isinstance(tool, str) or not isinstance(arguments, dict):
                return
            if not self._succeeded(result):
                return
            if tool in _FILE_TOOLS_READ:
                self._note_read(arguments.get("path"))
            elif tool in _FILE_TOOLS_MODIFIED:
                self._note_modified(arguments.get("path"))
            elif tool in _PROGRAM_TOOLS:
                self._note_read(arguments.get("file"))
            elif tool == "shell":
                command = arguments.get("command")
                if isinstance(command, str):
                    for match in _MC_EXEC_FILE.finditer(command):
                        self._note_read(match.group(1))
        except Exception:
            return

    @staticmethod
    def _succeeded(result: Any) -> bool:
        if isinstance(result, dict):
            if result.get("error"):
                return False
            if result.get("status") in {"tool_error", "skipped", "invalidated", "unknown",
                                        "workspace_quota_exceeded"}:
                return False
        return True

    def _note_read(self, path: Any) -> None:
        self._note(self.files_read, path)

    def _note_modified(self, path: Any) -> None:
        self._note(self.files_modified, path)

    @staticmethod
    def _note(bucket: list[str], path: Any) -> None:
        if not isinstance(path, str):
            return
        text = path.strip()
        if not text or len(text) > 512 or "\x00" in text:
            return
        if text in bucket:
            bucket.remove(text)
        bucket.append(text)
        del bucket[:-MAX_TRACKED_FILES]

    # -- projection summary --------------------------------------------
    def build_summary(self, *, handoff: Any = None, goal: str | None = None,
                      pending_executions: Any = None, pending_jobs: Any = None,
                      world_epoch: int = 0, runtime_epoch: int = 0) -> dict[str, Any]:
        """Build the deterministic compaction/branch summary fields."""
        handoff = handoff if isinstance(handoff, dict) else {}
        objective = self._text(handoff.get("objective"))
        if not objective:
            objective = self._text(goal) or self._text(self.goal) or ""
        work_in_progress = self._strings(
            handoff.get("work_in_progress"), MAX_LIST_ITEMS, MAX_ITEM_CHARS)
        next_steps = self._strings(
            handoff.get("next_steps"), MAX_LIST_ITEMS, MAX_ITEM_CHARS)
        if not next_steps:
            next_steps = self._strings(handoff.get("plans"), MAX_LIST_ITEMS, MAX_ITEM_CHARS)
        decisions = self._merge_unique(
            self.decisions,
            self._strings(handoff.get("decisions"), MAX_LIST_ITEMS, MAX_ITEM_CHARS))[-MAX_DECISIONS:]
        blockers = self._strings(handoff.get("blockers"), MAX_LIST_ITEMS, MAX_ITEM_CHARS)
        if not blockers:
            blockers = self._strings(handoff.get("unresolved_tasks"), MAX_LIST_ITEMS, MAX_ITEM_CHARS)
        return {
            "objective": objective[:1000],
            "work_in_progress": work_in_progress,
            "next_steps": next_steps,
            "decisions": decisions,
            "verified_facts": self._strings(handoff.get("verified_facts"),
                                            MAX_LIST_ITEMS, MAX_ITEM_CHARS),
            "failed_attempts": self._strings(handoff.get("failed_attempts"),
                                             MAX_LIST_ITEMS, MAX_ITEM_CHARS),
            "files_read": self._merge_files(
                self.files_read, self._strings(handoff.get("files_read"), MAX_TRACKED_FILES, 512)),
            "files_modified": self._merge_files(
                self.files_modified,
                self._strings(handoff.get("files_modified"), MAX_TRACKED_FILES, 512)),
            "blockers": blockers,
            "pending_executions": self._pending_executions(pending_executions),
            "pending_jobs": self._pending_jobs(pending_jobs),
            "world_epoch": world_epoch if type(world_epoch) is int and world_epoch >= 0 else 0,
            "runtime_epoch": runtime_epoch if type(runtime_epoch) is int and runtime_epoch >= 0 else 0,
        }

    @staticmethod
    def _text(value: Any, max_chars: int = 1000) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(value.split()).strip()[:max_chars]

    @staticmethod
    def _pending_executions(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, Any] = {}
        for key, handle in list(value.items())[:8]:
            if isinstance(key, str):
                result[key] = handle if isinstance(handle, (str, int)) else str(handle)
        return result

    @staticmethod
    def _pending_jobs(value: Any) -> list[str]:
        if not isinstance(value, (dict, list, tuple)):
            return []
        keys = list(value.keys()) if isinstance(value, dict) else list(value)
        return [str(key) for key in keys[:8] if isinstance(key, (str, int))]

    # -- journal records -------------------------------------------------
    def record_compaction(self, archive: str, summary: dict[str, Any],
                          retained_range: Any) -> None:
        """Persist one compaction projection (called after messages are rewritten)."""
        source = summary if isinstance(summary, dict) else {}
        # Merge projection state from the full summary before journal bounding.
        self.compactions += 1
        self.decisions = self._merge_unique(
            self.decisions,
            self._strings(source.get("decisions"), MAX_DECISIONS, MAX_ITEM_CHARS))[-MAX_DECISIONS:]
        self.files_read = self._merge_files(
            self._strings(source.get("files_read"), MAX_TRACKED_FILES, 512),
            self.files_read)[-MAX_TRACKED_FILES:]
        self.files_modified = self._merge_files(
            self._strings(source.get("files_modified"), MAX_TRACKED_FILES, 512),
            self.files_modified)[-MAX_TRACKED_FILES:]
        projection = {key: source[key] for key in (
            "objective", "work_in_progress", "next_steps", "decisions",
            "verified_facts", "failed_attempts", "files_read", "files_modified",
            "blockers", "pending_executions", "pending_jobs", "world_epoch",
            "runtime_epoch", "retained_start", "retained_end") if key in source}
        self.last_summary = self._bounded_mapping(projection, 16000)
        start = end = 0
        if isinstance(retained_range, (list, tuple)) and len(retained_range) == 2:
            start, end = (retained_range[0], retained_range[1])
        self._append_journal({
            "kind": "compaction",
            "agent_id": self.agent_id,
            "archive": str(archive)[:300],
            "retained_range": [int(start), int(end)],
            "compactions": self.compactions,
            "summary": self.last_summary,
        })

    def record_recovery(self, reason: str, details: Any = None) -> None:
        """Persist one branch/recovery projection."""
        self.recoveries += 1
        self._append_journal({
            "kind": "recovery",
            "agent_id": self.agent_id,
            "reason": str(reason or "restart")[:500],
            "recoveries": self.recoveries,
            "details": self._bounded_details(details),
            "files_read": list(self.files_read),
            "files_modified": list(self.files_modified),
        })

    def recovery_message(self, reason: str, *, previous_epoch: Any = None,
                         world_epoch: int = 0, runtime_epoch: int = 0,
                         pending_executions: Any = None, pending_jobs: Any = None,
                         next_steps: Any = None, globals_may_be_lost: bool = True,
                         details: Any = None) -> dict[str, Any]:
        """Structured branch summary emitted once when a conversation resumes."""
        previous: list[int] | None = None
        if isinstance(previous_epoch, (list, tuple)) and len(previous_epoch) == 2 and all(
                type(item) is int and item >= 0 for item in previous_epoch):
            previous = [previous_epoch[0], previous_epoch[1]]
        epoch_changed = previous is not None and tuple(previous) != (
            int(world_epoch), int(runtime_epoch))
        summary = self.last_summary if isinstance(self.last_summary, dict) else {}
        steps = self._strings(next_steps, 8, 300)
        if not steps:
            steps = [str(item)[:300] for item in (summary.get("next_steps") or [])][:8]
        if not steps:
            steps = ["Read your durable notes and verify the actual current state before acting."]
        files_read = list(summary.get("files_read") or []) or list(self.files_read)
        files_modified = list(summary.get("files_modified") or []) or list(self.files_modified)
        message = {
            "kind": "branch_summary",
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "agent_id": self.agent_id,
            "reason": str(reason or "restart")[:500],
            "notice": ("The harness reconstructed this conversation from its durable checkpoint; "
                       "this is not a fresh observation."),
            "epoch_changed": bool(epoch_changed),
            "previous_epoch": previous,
            "world_epoch": world_epoch if type(world_epoch) is int and world_epoch >= 0 else 0,
            "runtime_epoch": runtime_epoch if type(runtime_epoch) is int and runtime_epoch >= 0 else 0,
            "globals_may_be_lost": bool(globals_may_be_lost),
            "requires_reobservation": True,
            "pending_executions": self._pending_executions(pending_executions),
            "pending_jobs": self._pending_jobs(pending_jobs),
            "next_steps": [str(item)[:300] for item in steps][:8],
            "files_read": [str(item)[:300] for item in list(files_read)[-16:]],
            "files_modified": [str(item)[:300] for item in list(files_modified)[-16:]],
            "do_not_replay": ("Uncertain actions from before the restart must not be replayed; "
                              "observe the actual state before deciding."),
            "details": self._bounded_details(details),
        }
        return self._fit_recovery_message(message)

    @staticmethod
    def _fit_recovery_message(message: dict[str, Any]) -> dict[str, Any]:
        """Bound the branch summary so it never dominates the next request."""
        def encoded_size(value: Any) -> int:
            return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))

        if encoded_size(message) <= MAX_RECOVERY_MESSAGE_BYTES:
            return message
        message.pop("details", None)
        if encoded_size(message) <= MAX_RECOVERY_MESSAGE_BYTES:
            return message
        for key in ("files_read", "files_modified"):
            message[key] = message[key][-4:]
        for key in ("next_steps",):
            message[key] = message[key][:3]
        if encoded_size(message) <= MAX_RECOVERY_MESSAGE_BYTES:
            return message
        message["files_read"] = []
        message["files_modified"] = []
        message["next_steps"] = message["next_steps"][:1]
        return message
