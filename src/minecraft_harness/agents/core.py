from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import random
import re
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from .context import ContextEngine
from .tools import WorkspaceTools, tool_schemas, _tool
from .web import WebTools
from ..budget import BudgetExhausted


class ModelAdmissionError(RuntimeError):
    """Durable admission or request recording failed before provider invocation."""


class AgentCore:
    """Shared durable coding-agent mechanics for players and the read-only observer.

    This is a project design; the paper does not disclose its exact compactor.
    """

    def __init__(self, config: dict[str, Any], run_dir: Path, recorder: Any,
                 model: Any, budget: Any, *, role: str = "player",
                 state_root: Path | None = None, workspace: Any = None,
                 artifact_api: Any = None):
        self.config, self.recorder = config, recorder
        self.model, self.budget, self.role = model, budget, role
        self.run_dir = Path(run_dir)
        self.agent_id = config["agent_id"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.agent_id) or self.agent_id in {".", ".."}:
            raise ValueError("Invalid agent_id")
        # The runner process owns only this agent's state directory; in-process
        # runs keep the historical layout. Artifacts and the ledger stay in the
        # control plane when an artifact API is supplied.
        self.state_dir = Path(state_root) if state_root is not None else Path(run_dir) / "agents" / self.agent_id
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = self.state_dir / "checkpoint.json"
        self.artifact_api = artifact_api
        # Deterministic Pi-style context projection (files/WIP summary/journal).
        self.context = ContextEngine(self.state_dir, agent_id=self.agent_id,
                                     goal=config.get("goal", ""))
        if workspace is not None:
            self.workspace = workspace
        else:
            self.workspace = WorkspaceTools(Path(run_dir) / "workspaces" / self.agent_id,
                                            config.get("shell_enabled", False), config.get("shell_container"),
                                            config.get("max_workspace_bytes", 64_000_000),
                                            config.get("max_workspace_files", 4096),
                                            shell_env=config.get("shell_env"),
                                            max_shell_output_bytes=config.get("max_shell_output_bytes", 65536),
                                            max_shell_timeout_s=config.get("max_shell_timeout_s", 120),
                                            default_shell_timeout_s=config.get("default_shell_timeout_s", 120))
        shell_env = getattr(self.workspace, "shell_env", None) or {}
        if role == "observer" and shell_env:
            raise ValueError("Observer must not receive player submission credentials")
        if shell_env and shell_env.get("MC_AGENT_ID") != self.agent_id:
            raise ValueError("Workspace submission identity does not match this agent")
        self.paper_profile = config.get("profile") == "paper_aligned"
        self.paper_single_program = bool(config.get("paper_single_program", False))
        self.paper_tight_interface = bool(config.get("paper_tight_interface", False))
        self.paper_shell_cli = bool(config.get("paper_shell_cli", False))
        self.paper_cli_output = bool(config.get("paper_cli_output", False))
        if self.paper_profile and self.workspace.shell_enabled:
            # Paper §2.1: the player is one generic coding agent whose game
            # interface is the single-program CLI inside its shell. Force the
            # whole paper surface from the profile itself so a legacy template
            # that forgot one flag can never fall back to direct mc_* tools or
            # the harness's structured execution record.
            self.paper_single_program = True
            self.paper_tight_interface = True
            self.paper_shell_cli = True
            self.paper_cli_output = True
        elif self.paper_profile and (self.paper_single_program or self.paper_shell_cli):
            # Legacy/synthetic single-program fixtures may not have a shell but
            # still print the paper CLI text form (logs + returned value).
            self.paper_cli_output = True
        self.leftover_policy = str(config.get("leftover_policy", "reset"))
        self.schemas = tool_schemas(
            self.workspace.shell_enabled,
            config.get("lifecycle") == "paper_aligned" and not self.paper_single_program,
            config.get("web", {}).get("enabled", False),
            paper_single_program=self.paper_single_program,
            leftover_policy=self.leftover_policy,
            paper_tight_interface=self.paper_tight_interface,
            paper_shell_cli=self.paper_shell_cli,
            paper_profile=self.paper_profile,
        )
        self.pending_jobs: dict[str, dict[str, Any]] = {}
        self.handoff: dict[str, Any] = {}
        self._compaction_prepared = False
        self.web = WebTools(config.get("web", {}), budget=self.budget,
                            agent_id=self.agent_id)
        self.turns = 0
        self.no_progress_turns = 0
        self.event_cursor = 0
        self.pending_tools: list[dict[str, Any]] = []
        self.pending_executions: dict[str, str] = {}
        self.archives: list[str] = []
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": self._prompt()}]
        self.world_epoch = 0
        self.runtime_epoch = 0
        self.progress_fingerprints: list[str] = []
        self.deferred_events: list[dict[str, Any]] = []
        self.model_failures = 0
        self._resume()
        if artifact_api is not None:
            self.artifact_index = None
        else:
            self.artifact_index = self.state_dir / "artifact-access.sqlite3"
            with closing(sqlite3.connect(self.artifact_index)) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS owned_artifacts(hash TEXT PRIMARY KEY)")
                connection.commit()


    def _append(self, kind: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.recorder.append(kind, payload, actor_id=self.agent_id,
                                    world_epoch=self.world_epoch, runtime_epoch=self.runtime_epoch, **kwargs)

    def _artifact(self, value: Any) -> dict[str, Any]:
        reference = self.recorder.put_artifact(json.dumps(value, ensure_ascii=False, default=str).encode())
        self._grant_artifact(reference)
        return reference

    def _checkpoint(self, status: str = "running") -> None:
        state = {"schema_version": 1, "pending_jobs": self.pending_jobs, "handoff": self.handoff, "role": self.role, "agent_id": self.agent_id, "status": status,
                 "turns": self.turns, "no_progress_turns": self.no_progress_turns,
                 "messages": self.messages, "pending_tools": self.pending_tools,
                 "pending_executions": self.pending_executions, "event_cursor": self.event_cursor,
                 "archives": self.archives, "world_epoch": self.world_epoch,
                 "runtime_epoch": self.runtime_epoch, "progress_fingerprints": self.progress_fingerprints,
                 "deferred_events": self.deferred_events, "model_failures": self.model_failures,
                 "context": self.context.state()}
        temporary = self.checkpoint_path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.checkpoint_path)
        descriptor = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _resume(self) -> None:
        if not self.checkpoint_path.exists():
            return
        saved = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        if saved.get("agent_id") != self.agent_id or saved.get("schema_version") != 1:
            raise ValueError("Checkpoint does not match agent or schema")
        for name in ("turns", "no_progress_turns", "messages", "pending_tools", "pending_executions",
                     "pending_jobs", "handoff", "event_cursor", "archives", "world_epoch", "runtime_epoch", "progress_fingerprints", "deferred_events", "model_failures"):
            setattr(self, name, saved.get(name, getattr(self, name)))
        for name in ("world_epoch", "runtime_epoch"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                setattr(self, name, 0)
        self.context.load_state(saved.get("context"))

    async def _stopped(self) -> bool:
        result = self.budget.stop_requested()
        return bool(await result if inspect.isawaitable(result) else result)

    async def _await_admission(self) -> None:
        if hasattr(self.budget, "admission"):
            await self.budget.admission.wait()

    async def _backoff(self, failures: int) -> None:
        base = float(self.config.get("retry_base_s", 1))
        maximum = float(self.config.get("retry_max_s", 30))
        delay = min(maximum, base * 2 ** min(failures - 1, 10)) * random.uniform(0.5, 1.0)
        # Bounded slices make operator stops responsive during a provider outage.
        while delay > 0 and not await self._stopped():
            part = min(delay, 0.2)
            await asyncio.sleep(part)
            delay -= part

    def _paper_program_text(self, result: dict[str, Any]) -> str:
        """Render a program result as the paper CLI would: logs then returned value."""
        lines = []
        logs = result.get("logs")
        if isinstance(logs, list):
            for entry in logs:
                if isinstance(entry, dict):
                    message = entry.get("message")
                else:
                    message = entry
                if message is not None:
                    lines.append(str(message))
        returned = result.get("result")
        if returned is not None:
            if isinstance(returned, str):
                lines.append('Returned: ' + returned)
            else:
                lines.append('Returned: ' + json.dumps(returned, ensure_ascii=False, default=str))
        error = result.get("error")
        if isinstance(error, dict):
            lines.append('Error: ' + str(error.get("message", error)))
        elif error:
            lines.append('Error: ' + str(error))
        status = result.get("status")
        if status and status not in {"succeeded", "done"}:
            lines.append('Status: ' + str(status))
        cleanup = result.get("cleanup")
        if result.get("output_truncated") or (isinstance(cleanup, dict) and cleanup.get("logs_truncated")):
            lines.append('[program output was truncated by the runtime; full output is incomplete]')
        return "\n".join(lines) if lines else "(program produced no output)"

    def _paper_text_limit(self) -> int:
        """Paper CLI output is bounded by the model context, not a tool-result cap.

        A small reserve keeps the system prompt and the tool bundle representable
        after this message is appended; the paper itself has no disclosed output
        limit, so this is the unavoidable model-context bound rather than the
        smaller ``max_tool_result_chars`` display cap.
        """
        context = max(4000, int(self.config.get("context_chars", 60000)))
        system = len(json.dumps(self.messages[:1], ensure_ascii=False)) + 2000
        return max(1000, context - system)

    def _tool_result(self, call: dict[str, Any], result: dict[str, Any]) -> None:
        if self.paper_shell_cli and call["function"]["name"] == "shell" and isinstance(result, dict):
            text = str(result.get("output", ""))
            reference = self._artifact({"output": text, "exit_code": result.get("exit_code"),
                                        "truncated": result.get("truncated", False)})
            truncated = len(text) > self._paper_text_limit()
            self._append("tool_result", {"tool_call_id": call["id"], "tool": "shell",
                                         "result_ref": reference, "format": "paper_cli_text",
                                         "truncated": truncated})
            if truncated:
                text = text[:self._paper_text_limit()] + (
                    "\n[output exceeded the model context budget; full text retained as evidence]")
            self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": text})
            return
        if isinstance(result, dict) and "__paper_cli_text__" in result:
            text = str(result["__paper_cli_text__"])
            reference = self._artifact({"output": text})
            truncated = len(text) > self._paper_text_limit()
            self._append("tool_result", {"tool_call_id": call["id"], "tool": call["function"]["name"],
                                         "result_ref": reference, "format": "paper_cli_text",
                                         "truncated": truncated})
            if truncated:
                text = text[:self._paper_text_limit()] + (
                    "\n[output exceeded the model context budget; full text retained as evidence]")
            self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": text})
            return
        reference = self._artifact(result)
        self._append("tool_result", {"tool_call_id": call["id"], "tool": call["function"]["name"], "result_ref": reference})
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        result_cap = int(self.config.get("max_tool_result_chars", 16000))
        context_cap = max(1000, int(self.config.get("context_chars", 60000)) // 4)
        if self.paper_tight_interface:
            # Paper's CLI prints the program's logs/returned value; paper-tight
            # mode keeps a larger excerpt instead of exposing artifact_read.
            context_cap = max(context_cap, int(self.config.get("context_chars", 60000)) // 2)
        maximum = min(result_cap, context_cap)
        if len(encoded) > maximum:
            summary = {"truncated": True}
            if not self.paper_tight_interface:
                summary["artifact"] = reference
                summary["message"] = "Full output retained; use artifact_read(hash,offset,limit) to read your original result."
            else:
                summary["message"] = "Output exceeded the paper-tight display cap; keep this bounded excerpt and make the next program log less."
            # Keep execution handles and pagination available even when the
            # content/log body is large. A truncated excerpt alone loses them.
            for key in ("status", "execution_id", "request_id", "offset", "next_offset", "revision"):
                value = result.get(key)
                if value is None or isinstance(value, (int, bool)):
                    if key in result:
                        summary[key] = value
                elif isinstance(value, str):
                    summary[key] = value[:200]
            excerpt_limit = max(0, maximum - len(json.dumps(summary, ensure_ascii=False)) - 30)
            summary["excerpt"] = encoded[:excerpt_limit]
            encoded = json.dumps(summary, ensure_ascii=False)
            # Escaping the excerpt adds JSON overhead; reduce until the actual
            # tool message, rather than its inner string, satisfies the cap.
            while len(encoded) > maximum and summary["excerpt"]:
                summary["excerpt"] = summary["excerpt"][:max(0, len(summary["excerpt"]) - (len(encoded) - maximum))]
                encoded = json.dumps(summary, ensure_ascii=False)
        self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": encoded})

    def _compress(self) -> None:
        limit = max(4000, int(self.config.get("context_chars", 60000)))
        if (len(json.dumps(self.messages, ensure_ascii=False)) <= limit and not self._compaction_prepared) or self.pending_tools:
            return
        # Archive even a single oversized user/event message or assistant bundle.
        # Keeping the latest bundle unconditionally allows unbounded requests.
        if len(json.dumps([self.messages[0]], ensure_ascii=False)) > limit - 600:
            raise ValueError("System prompt exceeds the conversation limit; shorten the experiment goal")
        old = self.messages[1:]
        encoded = json.dumps(old, ensure_ascii=False)
        archive = "history-" + hashlib.sha256(encoded.encode()).hexdigest()[:20] + ".json"
        descriptor, temporary = tempfile.mkstemp(prefix=".history-", dir=self.state_dir)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_dir / archive)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        if archive not in self.archives:
            self.archives.append(archive)
        self.archives = self.archives[-64:]
        intentions = [message.get("content") or "" for message in old if message["role"] == "assistant"][-3:]
        handoff = self.handoff or self._fallback_handoff()
        # Pi-style projection: the deterministic summary carries objective, WIP,
        # next steps, decisions, verified/failed work, file effects, blockers and
        # pending handles so a compaction is a software-work hand-off rather than
        # a harness bookkeeping dump.
        projection = self.context.build_summary(
            handoff=handoff, goal=self.config.get("goal", ""),
            pending_executions=self.pending_executions,
            pending_jobs=self.pending_jobs,
            world_epoch=self.world_epoch, runtime_epoch=self.runtime_epoch)
        summary = {
            **projection,
            "notice": ("Older messages were archived from the active conversation. "
                       "This deterministic checkpoint is not a fresh observation."),
            "event_cursor": self.event_cursor,
            "recent_agent_intentions_unverified": [str(item)[-150:] for item in intentions],
            "handoff_ref": self._artifact(handoff),
            "handoff": handoff,
            "pending_job_count": len(self.pending_jobs),
            "pending_execution_count": len(self.pending_executions),
            "next_step": ("Read notes/source and verify game state before continuing."
                          if self.paper_tight_interface else
                          "Read notes/source/history and mc_pending for unresolved execution handles; verify game state before continuing."),
        }
        if not self.paper_tight_interface:
            summary["archive"] = archive
            summary["access"] = "history_read"
        summary = self._fit_compaction_summary(summary, limit)
        messages = [self.messages[0], {"role": "user", "content": json.dumps(summary, ensure_ascii=False)}]
        if len(json.dumps(messages, ensure_ascii=False)) > limit:
            raise ValueError("Conversation limit cannot hold the system prompt and recovery state")
        # Keep a recent suffix only when the entire assistant/tool bundle fits.
        # Leave room for several turns. Refilling to the hard limit causes an
        # archive + fsync on almost every subsequent request in a long run.
        target = max(len(json.dumps(messages, ensure_ascii=False)), int(limit * .6))
        starts = [index for index, message in enumerate(self.messages) if index > 0 and message["role"] == "assistant"]
        retained_start = len(old)
        for cut in starts:
            candidate = messages + self.messages[cut:]
            if len(json.dumps(candidate, ensure_ascii=False)) <= target:
                messages = candidate
                retained_start = cut - 1  # archived messages start at messages[1]
                break
        self.messages = messages
        self._compaction_prepared = False
        retained_range = [max(0, retained_start), len(old)]
        self._append("context_compacted", {"archive": archive, "original_ref": self._artifact(old),
                                           "summary": summary, "retained_range": retained_range})
        self.context.record_compaction(archive, summary, retained_range)
        self._checkpoint()

    _COMPACTION_LIST_FIELDS = ("work_in_progress", "next_steps", "decisions",
                               "verified_facts", "failed_attempts", "files_read",
                               "files_modified", "blockers")

    def _fit_compaction_summary(self, summary: dict[str, Any], limit: int) -> dict[str, Any]:
        """Bound the compaction summary so the next request always fits.

        Recovery-critical fields (epochs, pending handles, handoff reference) are
        kept even in the smallest projection; descriptive lists are trimmed
        first. The full handoff is always durably available through handoff_ref.
        """
        system = self.messages[0]

        def encoded_size(value: dict[str, Any]) -> int:
            # Measure the real request message: the summary is JSON text nested
            # inside the message, so quote escaping must be counted too.
            message = {"role": "user", "content": json.dumps(value, ensure_ascii=False)}
            return len(json.dumps([system, message], ensure_ascii=False))

        def fits(value: dict[str, Any]) -> bool:
            return encoded_size(value) <= limit

        candidate = dict(summary)
        if not fits(candidate):
            candidate.pop("handoff", None)
            candidate["handoff_omitted"] = "Full handoff is retained through handoff_ref."
        if not fits(candidate):
            original = candidate.get("recent_agent_intentions_unverified")
            if isinstance(original, list):
                candidate["recent_agent_intentions_unverified"] = [item[:100] for item in original[:2]]
        for max_items in (8, 4, 2, 1):
            if fits(candidate):
                return candidate
            for field in self._COMPACTION_LIST_FIELDS:
                value = candidate.get(field)
                if isinstance(value, list) and len(value) > max_items:
                    candidate[field] = [str(item)[:200] for item in value[:max_items]]
        if fits(candidate):
            return candidate
        # Last resort: the scalars and pending handles needed for safe recovery.
        minimal = {key: candidate[key] for key in (
            "objective", "world_epoch", "runtime_epoch", "event_cursor",
            "pending_execution_count", "pending_job_count", "handoff_ref",
            "notice", "next_step", "archive", "access", "handoff_omitted") if key in candidate}
        pending_executions = candidate.get("pending_executions")
        if isinstance(pending_executions, dict):
            minimal["pending_executions"] = dict(list(pending_executions.items())[:2])
        pending_jobs = candidate.get("pending_jobs")
        if isinstance(pending_jobs, list):
            minimal["pending_jobs"] = [str(item)[:100] for item in pending_jobs[:2]]
        if fits(minimal):
            return minimal
        # The system prompt and a few recovery scalars must always be the floor.
        smallest = {key: candidate[key] for key in (
            "world_epoch", "runtime_epoch", "event_cursor",
            "pending_execution_count", "pending_job_count") if key in candidate}
        if isinstance(pending_executions, dict):
            smallest["pending_executions"] = dict(list(pending_executions.items())[:2])
        if isinstance(pending_jobs, list):
            smallest["pending_jobs"] = [str(item)[:100] for item in pending_jobs[:2]]
        return smallest

    def _recovery_branch_message(self, reason: str, *,
                                 previous_epoch: tuple[int, int] | None = None,
                                 details: Any = None) -> dict[str, Any]:
        """One structured branch/recovery summary emitted when a run resumes."""
        handoff = self.handoff if isinstance(self.handoff, dict) else {}
        message = self.context.recovery_message(
            reason,
            previous_epoch=previous_epoch,
            world_epoch=self.world_epoch,
            runtime_epoch=self.runtime_epoch,
            pending_executions=self.pending_executions,
            pending_jobs=self.pending_jobs,
            next_steps=handoff.get("next_steps") or handoff.get("plans"),
            globals_may_be_lost=True,
            details=details,
        )
        self.context.record_recovery(reason, message.get("details", details))
        self._append("context_branch_summary", {
            "reason": message["reason"],
            "epoch_changed": message["epoch_changed"],
            "previous_epoch": message.get("previous_epoch"),
            "world_epoch": self.world_epoch,
            "runtime_epoch": self.runtime_epoch,
            "pending_execution_count": len(self.pending_executions),
            "pending_job_count": len(self.pending_jobs),
        })
        return message

    def _grant_artifact(self, reference: Any) -> None:
        digest = reference.get("hash", reference.get("sha256")) if isinstance(reference, dict) else None
        if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest):
            if self.artifact_api is not None:
                self.artifact_api.grant(self.agent_id, digest)
                return
            with closing(sqlite3.connect(self.artifact_index)) as connection:
                connection.execute("INSERT OR IGNORE INTO owned_artifacts VALUES(?)", (digest,))
                connection.commit()

    def _artifact_owned(self, digest: str) -> bool:
        if self.artifact_api is not None:
            return bool(self.artifact_api.owned(self.agent_id, digest))
        with closing(sqlite3.connect(self.artifact_index)) as connection:
            return connection.execute("SELECT 1 FROM owned_artifacts WHERE hash=?", (digest,)).fetchone() is not None

    def _artifact_read(self, hash: str, offset: int = 0, limit: int = 12000) -> dict[str, Any]:
        if not isinstance(hash, str) or not re.fullmatch(r"[a-f0-9]{64}", hash):
            raise ValueError("Artifact hash must be SHA256")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 32000:
            raise ValueError("Artifact offset must be nonnegative and limit between 1 and 32000")
        if self.artifact_api is not None:
            return self.artifact_api.read(self.agent_id, hash, offset, limit)
        with closing(sqlite3.connect(self.artifact_index)) as connection:
            allowed = connection.execute("SELECT 1 FROM owned_artifacts WHERE hash=?", (hash,)).fetchone()
        if not allowed and hasattr(self.recorder, "db") and hasattr(self.recorder, "lock"):
            # Legacy checkpoints predate the private ACL index. Recover access
            # lazily from trusted own top-level result/request references, never
            # by scanning untrusted chat text or another player's artifacts.
            with self.recorder.lock:
                allowed = self.recorder.db.execute("""
                    SELECT 1 FROM events WHERE actor_id=? AND kind IN
                    ('model_request','model_response','tool_result','context_compacted','context_handoff')
                    AND (json_extract(payload_json,'$.request_ref.hash')=?
                      OR json_extract(payload_json,'$.response_ref.hash')=?
                      OR json_extract(payload_json,'$.result_ref.hash')=?
                      OR json_extract(payload_json,'$.original_ref.hash')=?
                      OR json_extract(payload_json,'$.handoff_ref.hash')=?) LIMIT 1
                    """, (self.agent_id, hash, hash, hash, hash, hash)).fetchone()
            if allowed:
                self._grant_artifact({"hash": hash})
        if not allowed:
            raise PermissionError("Only artifacts already delivered to this agent are readable")
        artifact_dir = getattr(self.recorder, "artifact_dir", self.run_dir / "artifacts")
        descriptor = os.open(Path(artifact_dir) / hash, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise PermissionError("Artifact is not a regular file")
            digest = hashlib.sha256()
            while chunk := handle.read(128000):
                digest.update(chunk)
            if digest.hexdigest() != hash:
                raise ValueError("Artifact integrity violation")
            handle.seek(offset)
            content = handle.read(limit + 1)
        return {"hash": hash, "content": content[:limit].decode("utf-8", errors="replace"),
                "offset": offset, "next_offset": offset + limit if len(content) > limit else None,
                "offset_unit": "utf8_bytes"}

    async def _model_call(self, messages: list[dict[str, Any]], schemas: list[dict[str, Any]],
                          *, purpose: str = "agent_decision",
                          tool_choice: dict[str, Any] | None = None) -> Any:
        """One durable admission/settlement for exactly one actual model attempt."""
        try:
            if hasattr(self.budget, "validate_request"):
                size = (self.model.request_bytes(messages, schemas) if hasattr(self.model, "request_bytes") else
                        len(json.dumps({"messages": messages, "tools": schemas}, ensure_ascii=False).encode()) + 4096)
                validation = self.budget.validate_request(size)
                if inspect.isawaitable(validation):
                    await validation
            request_document = {"messages": messages, "tools": schemas}
            if tool_choice is not None:
                request_document["tool_choice"] = tool_choice
            request_ref = self._artifact(request_document)
            parameters = inspect.signature(self.budget.reserve).parameters
            reservation = (await self.budget.reserve(self.agent_id, purpose=purpose, role=self.role)
                           if "purpose" in parameters else await self.budget.reserve(self.agent_id))
        except BudgetExhausted:
            raise
        except Exception as exc:
            raise ModelAdmissionError(type(exc).__name__) from exc
        sent = False
        try:
            self._append("model_request", {"turn": self.turns + 1, "request_ref": request_ref,
                                           "purpose": purpose, "role": self.role, "reservation_id": reservation})
            supports_tool_choice = "tool_choice" in inspect.signature(self.model.complete).parameters
            async with asyncio.timeout(float(self.config.get("model_request_timeout_s", 95))):
                sent = True
                response = (await self.model.complete(messages, schemas, tool_choice=tool_choice)
                            if supports_tool_choice else await self.model.complete(messages, schemas))
        except BaseException as exc:
            usage = ({"prompt_tokens": None, "completion_tokens": None, "total_tokens": None} if sent else
                     {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
            await self._settle_durably(reservation, usage)
            if not sent and isinstance(exc, Exception):
                raise ModelAdmissionError(type(exc).__name__) from exc
            raise
        await self._settle_durably(reservation, response.usage)
        try:
            self._append("model_response", {"turn": self.turns + 1, "response_ref": self._artifact(response.__dict__),
                                            "usage": response.usage, "latency_ms": response.latency_ms,
                                            "purpose": purpose, "role": self.role, "reservation_id": reservation})
        except Exception as exc:
            raise ModelAdmissionError(type(exc).__name__) from exc
        return response

    async def _settle_durably(self, reservation: Any, usage: dict[str, Any]) -> None:
        settlement = asyncio.ensure_future(self.budget.settle(reservation, usage))
        try:
            await asyncio.shield(settlement)
        except asyncio.CancelledError:
            # Do not leave budget writes running after Supervisor closes stores.
            await settlement
            raise
        except Exception as exc:
            raise ModelAdmissionError(type(exc).__name__) from exc

    def _fallback_handoff(self) -> dict[str, Any]:
        recent = []
        for message in self.messages[-12:]:
            if message.get("role") in {"assistant", "tool"}:
                recent.append({"role": message["role"], "content": str(message.get("content") or "")[-200:],
                               "tools": [item.get("function", {}).get("name") for item in message.get("tool_calls", [])]})
        return {"schema_version": 1, "complete": False, "source": "deterministic_fallback",
                "objective": self.config.get("goal", ""), "world_epoch": self.world_epoch,
                "runtime_epoch": self.runtime_epoch, "recent_unverified": recent,
                "pending_executions": dict(self.pending_executions), "pending_jobs": self.pending_jobs,
                "previous_handoff_ref": self._artifact(self.handoff) if self.handoff else None,
                "facts_require_reobservation": True,
                # Deterministic Pi-style projection fields (kept alongside the
                # historical handoff keys for compatibility).
                "work_in_progress": [],
                "next_steps": [],
                "decisions": list(self.context.decisions[-8:]),
                "files_read": list(self.context.files_read[-12:]),
                "files_modified": list(self.context.files_modified[-12:]),
                "blockers": [],
                "warning": "Handoff is incomplete. Read original history, notes and source; do not replay unknown actions."}

    async def _prepare_compaction(self) -> None:
        """Budgeted note/handoff phase, followed by deterministic archival in _compress.

        Strict legacy runs retain deterministic behavior. Aligned runs hand off
        near the threshold while full paired context still fits. Oversized event
        bursts use bounded excerpts and retain the original archive for recovery.
        """
        limit = max(4000, int(self.config.get("context_chars", 60000)))
        if (self.config.get("lifecycle") != "paper_aligned" or self.pending_tools or
                len(json.dumps(self.messages, ensure_ascii=False)) <= int(limit * .85) or await self._stopped()):
            return
        fallback = self._fallback_handoff()
        self.handoff = fallback
        fields = {name: {"type": "array", "items": {"type": "string"}, "maxItems": 20}
                  for name in ("plans", "verified_facts", "failed_attempts", "source_paths", "unresolved_tasks")}
        # Pi-style projection fields extend (but do not replace) the historical
        # handoff schema, so old checkpoints and fixtures stay compatible.
        projection_fields = {
            name: {"type": "array", "items": {"type": "string"}, "maxItems": 20}
            for name in ("work_in_progress", "next_steps", "decisions",
                         "files_read", "files_modified", "blockers")}
        handoff_schema = _tool(
            "context_handoff",
            "Write the durable context handoff. Facts must include time, epoch and evidence references; mark stale facts.",
            {"objective": {"type": "string"}, **fields, **projection_fields},
            ["objective", *fields])
        allowed_names = {"workspace_write", "memory_write"} if self.role == "player" else {"workspace_write"}
        allowed = [item for item in self.schemas if item["function"]["name"] in allowed_names]
        request = [{"role": "system", "content": (
                        "Prepare a context handoff for a long software-work session. First update your own notes or "
                        "memory with the work in progress and the exact next steps you will take; those notes survive "
                        "compaction. Then call context_handoff with the objective, plans, verified and failed work, "
                        "source references, blockers, files read or modified, and uncertain execution handles. "
                        "Do not invent game facts or execute game actions. Call context_handoff in this or the next response.")},
                   {"role": "user", "content": json.dumps({"recovery": fallback,
                     "recent_messages": [{**item, "content": str(item.get("content") or "")[-600:]}
                                         for item in self.messages[-6:]]}, ensure_ascii=False)}]
        # Reserve at least half the hard request context for schema/system overhead.
        while len(json.dumps(request, ensure_ascii=False)) > max(1800, limit // 2):
            payload = json.loads(request[-1]["content"])
            if payload.get("recent_messages"):
                payload["recent_messages"].pop(0)
            else:
                payload["recovery"] = {"objective": str(fallback["objective"])[:200], "world_epoch": self.world_epoch,
                                       "runtime_epoch": self.runtime_epoch, "incomplete": True,
                                       "pending_execution_ids": list(self.pending_executions)[:2],
                                       "pending_job_ids": list(self.pending_jobs)[:8],
                                       "previous_handoff_ref": fallback.get("previous_handoff_ref")}
                request[-1]["content"] = json.dumps(payload, ensure_ascii=False)
                break
            request[-1]["content"] = json.dumps(payload, ensure_ascii=False)
        recovery = {"objective": self.config.get("goal", ""), "agent_id": self.agent_id,
                    "world_epoch": self.world_epoch, "runtime_epoch": self.runtime_epoch,
                    "pending_executions": dict(self.pending_executions), "pending_job_ids": list(self.pending_jobs),
                    "files_read": list(self.context.files_read[-8:]),
                    "files_modified": list(self.context.files_modified[-8:]),
                    "work_in_progress": fallback.get("work_in_progress", []),
                    "next_steps": fallback.get("next_steps", [])}
        full_context_request = [request[0], *self.messages[1:], {"role": "user", "content":
            "Prepare the durable context handoff now. Authoritative recovery metadata (facts still require verification): "
            + json.dumps(recovery, ensure_ascii=False) +
            ". Record what you are working on, your next steps, and any files you read or changed. "
            "You may update your own notes first."}]
        if len(json.dumps(full_context_request, ensure_ascii=False)) <= limit - 1800:
            request = full_context_request
        base_request = list(request)
        try:
            for _ in range(2):
                response = await self._model_call(request, [handoff_schema, *allowed], purpose="context_compaction")
                request.append(response.as_message())
                valid = None
                note_updates = []
                for call in response.tool_calls:
                    name = call["function"]["name"]
                    try:
                        args = json.loads(call["function"]["arguments"])
                        if not isinstance(args, dict):
                            raise ValueError("Invalid handoff arguments")
                        if name == "context_handoff":
                            if (not isinstance(args.get("objective"), str) or len(args["objective"]) > 4000 or any(
                                    not isinstance(args.get(key), list) or len(args[key]) > 20 or
                                    any(not isinstance(value, str) or len(value) > 2000 for value in args[key]) for key in fields)):
                                raise ValueError("Incomplete handoff fields")
                            projected = {}
                            for optional in projection_fields:
                                if optional not in args:
                                    continue
                                values = args[optional]
                                if (not isinstance(values, list) or len(values) > 20 or
                                        any(not isinstance(value, str) or len(value) > 2000
                                            for value in values)):
                                    raise ValueError(f"Invalid handoff field: {optional}")
                                projected[optional] = values
                            valid = {"schema_version": 1, "complete": True, "source": "agent_handoff",
                                     **{key: args[key] for key in ("objective", *fields)},
                                     **projected,
                                     "world_epoch": self.world_epoch, "runtime_epoch": self.runtime_epoch,
                                     "pending_executions": dict(self.pending_executions), "pending_jobs": self.pending_jobs}
                            result = {"status": "saved"}
                        elif name in allowed_names:
                            result = await self._dispatch(name, args, "handoff:" + call["id"])
                            self.context.track_tool(name, args, result)
                            receipt = {"tool": name, "path": str(args.get("path", ""))[:200],
                                       "scope": str(args.get("scope", ""))[:20],
                                       "result_ref": self._artifact(result)}
                            note_updates.append(receipt)
                        else:
                            result = {"error": "Tool is unavailable during handoff"}
                    except (ValueError, PermissionError, FileNotFoundError) as exc:
                        result = {"error": str(exc)}
                    request.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, default=str)[:2000]})
                if valid:
                    self.handoff = valid
                    break
                updates = json.dumps(note_updates, ensure_ascii=False)
                if len(updates) > 1200:
                    updates = json.dumps({"updates_ref": self._artifact(note_updates),
                                          "paths": [item["path"][:100] for item in note_updates[:4]],
                                          "truncated": True}, ensure_ascii=False)
                request = base_request + [{"role": "user", "content":
                    "The note-writing phase is complete. Updates: " + updates +
                    ". Finish with context_handoff now. Original outputs are durably recorded. No game actions are allowed."}]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.handoff["failure_reason"] = type(exc).__name__
            self._append("context_handoff_incomplete", {"reason": type(exc).__name__, "fallback": True})
        self._compaction_prepared = True
        self._append("context_handoff", {"complete": self.handoff.get("complete", False),
                                         "failure_reason": self.handoff.get("failure_reason"),
                                         "stop_requested": await self._stopped(), "handoff_ref": self._artifact(self.handoff)})
        self._checkpoint()
