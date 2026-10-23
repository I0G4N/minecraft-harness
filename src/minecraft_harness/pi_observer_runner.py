"""Pi Observer runner process for the paper-aligned experiment.

The Python :class:`minecraft_harness.observer.agent.ObserverAgent` is replaced at
runtime by a real Pi coding-agent process with a deliberately small, read-only
observer tool surface:

* a loopback :class:`minecraft_harness.pi_model_proxy.PiModelProxy` provides the
  OpenAI-compatible endpoint and meters every model review through the runner
  RPC budget/ledger with ``role=observer`` and ``purpose=observer_review``;
* a loopback :class:`minecraft_harness.pi_observer_bridge.PiObserverBridge`
  exposes exactly ``records_read``, ``evidence_read``, ``observer_report`` and
  ``infra_issue`` to the Pi extension; the bridge forwards them over the runner
  RPC connection bound to the Observer identity;
* Pi runs as a long-lived ``--mode rpc`` subprocess with only
  ``read``/``write`` plus the four registered observer tools - no bash, no game
  client, no memory or web tools;
* Pi's session directory gives crash/resume continuity via ``--continue`` and is
  bounded with the shared ``pi_runner`` retention policy;
* the runner speaks ``runner.ready``/``runner.heartbeat``/``runner.finished`` so
  the Supervisor/RunnerManager control plane can supervise it like any runner.

The process never imports the Supervisor, Recorder, MemoryStore, workspaces or
Docker modules: it only uses the runner RPC client, the paper Observer prompt and
the model proxy.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import pi_runner as _pi
from .agents.prompts import observer_prompt, paper_observer_prompt
from .observer.agent import ObserverAgent
from .pi_model_proxy import ArtifactBudget, PiModelProxy
from .pi_observer_bridge import PiObserverBridge

PiObserverError = _pi.PiRunnerError

# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------

ENV_RUNNER_SOCKET = _pi.ENV_RUNNER_SOCKET
ENV_RUNNER_TOKEN = _pi.ENV_RUNNER_TOKEN
ENV_RUNNER_AGENT = _pi.ENV_RUNNER_AGENT
ENV_RUNNER_ROLE = _pi.ENV_RUNNER_ROLE
ENV_RUNNER_SPEC = _pi.ENV_RUNNER_SPEC
ENV_RUNNER_RUN_ID = _pi.ENV_RUNNER_RUN_ID

SPEC_SCHEMA_VERSION = _pi.SPEC_SCHEMA_VERSION
MAX_SPEC_BYTES = _pi.MAX_SPEC_BYTES
MAX_PATH_CHARS = _pi.MAX_PATH_CHARS
MAX_RUN_ID_CHARS = _pi.MAX_RUN_ID_CHARS

OBSERVER_TOOLS = ("read", "write", "records_read", "evidence_read",
                  "observer_report", "infra_issue")
OBSERVER_READ_ONLY_TOOLS = frozenset({"read", "records_read", "evidence_read"})
OBSERVER_EXTENSION_RELATIVE = Path("extensions") / "observer-tools.js"

DEFAULT_INTERVAL_S = 60.0
MIN_INTERVAL_S = 0.05
MAX_INTERVAL_S = 86_400.0
DEFAULT_MAX_REVIEWS = 100_000
MAX_REVIEWS = 100_000

INITIAL_PROMPT = (
    "Begin the review. Read the experiment records from the beginning, check the "
    "players' claims against those records, and publish a first report with a "
    "summary of progress, the verified events and your infrastructure findings."
)
RESUME_PROMPT = (
    "Resume the review after a restart. Re-read the records and your workspace notes, "
    "re-verify anything uncertain, then publish an updated report."
)
REVIEW_PROMPT = (
    "A new review is due. Read the new records since your last report, check claims and "
    "important events against the record, verify artifacts where needed, and publish an "
    "updated report. Keep useful notes in your workspace."
)

PI_OBSERVER_READY_TIMEOUT_SECONDS = _pi.READY_TIMEOUT_SECONDS
PI_OBSERVER_FINISHED_TIMEOUT_SECONDS = _pi.FINISHED_TIMEOUT_SECONDS
PI_OBSERVER_ABORT_GRACE_SECONDS = _pi.PI_ABORT_GRACE_SECONDS
PI_OBSERVER_TERMINATE_GRACE_SECONDS = _pi.PI_TERMINATE_GRACE_SECONDS
PI_OBSERVER_STREAM_LIMIT = _pi.PI_STREAM_LIMIT
PI_OBSERVER_MAX_TOOL_ARTIFACT_BYTES = 1024 * 1024
PI_OBSERVER_MAX_COMPACTION_ARTIFACT_BYTES = 512 * 1024
SESSION_GUARD_SECONDS = 30.0

IMPORTANT_KINDS = frozenset(ObserverAgent.IMPORTANT_KINDS) | frozenset({
    "agent_degraded", "observer_degraded", "success_candidate",
    "first_success_verified", "task_success", "server_unavailable",
    "runtime_unavailable",
})

_FORWARDED_CHILD_ENV_KEYS = frozenset({
    "PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "PYTHONPATH", "PYTHONHOME", "TMPDIR",
})
_AGENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,32}")


# ---------------------------------------------------------------------------
# Environment and spec
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PiObserverSpec:
    run_id: str
    agent_id: str
    role: str
    state_root: Path
    workspace_root: Path
    reference_root: Path
    pi_cli: Path
    pi_context_window: int
    pi_session_max_bytes: int
    pi_artifact_max_bytes: int
    heartbeat_s: float
    interval_s: float
    max_reviews: int
    agent_config: dict[str, Any]
    pi_model: dict[str, Any]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_spec(environment: _pi.PiEnvironment) -> PiObserverSpec:
    """Read and validate the Observer runner spec without following a symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(environment.spec_path, flags)
    except OSError as exc:
        raise PiObserverError(
            f"Runner spec is not readable: {exc.strerror or type(exc).__name__}") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PiObserverError("Runner spec must be a regular file")
        if metadata.st_size > MAX_SPEC_BYTES:
            raise PiObserverError("Runner spec exceeds the size limit")
        handle = os.fdopen(descriptor, "rb")
        descriptor = -1
        with handle:
            raw = handle.read(MAX_SPEC_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > MAX_SPEC_BYTES:
        raise PiObserverError("Runner spec exceeds the size limit")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PiObserverError("Runner spec is not valid UTF-8 JSON") from None
    return _validate_spec(document, environment)


def _validate_spec(document: Any, environment: _pi.PiEnvironment) -> PiObserverSpec:
    if not isinstance(document, dict):
        raise PiObserverError("Runner spec must be a JSON object")
    version = document.get("schema_version")
    if type(version) is not int or version != SPEC_SCHEMA_VERSION:
        raise PiObserverError("Unsupported runner spec schema version")
    runtime = document.get("runner_runtime")
    if runtime is not None and runtime not in {"pi", "pi_observer", "pi-observer"}:
        raise PiObserverError("pi_observer_runner requires runner_runtime=pi")
    run_id = document.get("run_id")
    if not isinstance(run_id, str) or not 1 <= len(run_id) <= MAX_RUN_ID_CHARS:
        raise PiObserverError("Runner spec needs a bounded run_id")
    if environment.run_id is not None and environment.run_id != run_id:
        raise PiObserverError("Runner spec run_id does not match HARNESS_RUNNER_RUN_ID")
    if document.get("agent_id") != environment.agent_id:
        raise PiObserverError("Runner spec agent_id does not match HARNESS_RUNNER_AGENT")
    role = document.get("role")
    if role != environment.role:
        raise PiObserverError("Runner spec role does not match HARNESS_RUNNER_ROLE")
    if role != "observer":
        raise PiObserverError("pi_observer_runner only supports role=observer")
    if environment.agent_id != "observer":
        # The observer RPC contract is bound to the Observer identity (role=observer
        # and agent_id=observer); fail closed rather than run with unusable tools.
        raise PiObserverError(
            "pi_observer_runner requires HARNESS_RUNNER_AGENT=observer")
    # The Observer has no game client and no shell, so player CLI credentials and
    # a game broker socket must never be present in its spec.
    if document.get("cli_environment") not in (None, {}):
        raise PiObserverError("Observer runner spec must not carry player CLI credentials")
    if document.get("broker_socket") not in (None, ""):
        raise PiObserverError("Observer runner spec must not carry a game broker socket")
    state_root = _pi._absolute_path(document.get("state_root"),
                                    "Runner spec state_root")
    workspace = document.get("workspace_root")
    workspace_root = (_pi._absolute_path(workspace, "Runner spec workspace_root")
                      if workspace is not None else state_root / "workspace")
    reference = document.get("reference_root")
    reference_root = (_pi._absolute_path(reference, "Runner spec reference_root")
                      if reference is not None else workspace_root / "reference")
    pi_model = _pi._scrub_pi_model(document.get("pi_model"))
    agent_config = _pi._scrub_agent_config(document.get("agent_config"),
                                           agent_id=environment.agent_id)
    observer_config = document.get("observer")
    if isinstance(observer_config, dict):
        for key, value in observer_config.items():
            if key not in {"interval_s", "max_reviews", "max_turns"}:
                continue
            if key == "max_turns" and "max_reviews" in agent_config:
                continue
            agent_config.setdefault(key, value)
    pi_cli = _pi._resolve_pi_cli(document.get("pi_cli"))
    context_window = document.get("pi_context_window")
    if context_window is None:
        context_window = os.environ.get(_pi.ENV_PI_CONTEXT_WINDOW)
    if context_window is None:
        context_window = _pi.DEFAULT_PI_CONTEXT_WINDOW
    if (isinstance(context_window, bool)
            or not isinstance(context_window, int)
            or not _pi.MIN_PI_CONTEXT_WINDOW <= context_window <= _pi.MAX_PI_CONTEXT_WINDOW):
        raise PiObserverError("Runner spec pi_context_window is out of range")
    pi_session_max_bytes = document.get("pi_session_max_bytes",
                                        _pi.DEFAULT_PI_SESSION_MAX_BYTES)
    if (type(pi_session_max_bytes) is not int
            or not _pi.MIN_RETENTION_BYTES <= pi_session_max_bytes
            <= _pi.MAX_RETENTION_BYTES):
        raise PiObserverError("Runner spec pi_session_max_bytes is out of range")
    pi_artifact_max_bytes = document.get("pi_artifact_max_bytes",
                                         _pi.DEFAULT_PI_ARTIFACT_MAX_BYTES)
    if (type(pi_artifact_max_bytes) is not int
            or not _pi.MIN_RETENTION_BYTES <= pi_artifact_max_bytes
            <= _pi.MAX_RETENTION_BYTES):
        raise PiObserverError("Runner spec pi_artifact_max_bytes is out of range")
    heartbeat_s = document.get("heartbeat_s", _pi.DEFAULT_HEARTBEAT_SECONDS)
    if isinstance(heartbeat_s, bool) or not isinstance(heartbeat_s, (int, float)):
        raise PiObserverError("Runner spec heartbeat_s must be a number")
    heartbeat_s = float(heartbeat_s)
    if not heartbeat_s == heartbeat_s or not (
            _pi.MIN_HEARTBEAT_SECONDS <= heartbeat_s <= _pi.MAX_HEARTBEAT_SECONDS):
        raise PiObserverError("Runner spec heartbeat_s is out of range")
    interval_s = document.get("interval_s", agent_config.get("interval_s",
                                                             DEFAULT_INTERVAL_S))
    if isinstance(interval_s, bool) or not isinstance(interval_s, (int, float)):
        raise PiObserverError("Observer interval_s must be a number")
    interval_s = float(interval_s)
    if not interval_s == interval_s or not MIN_INTERVAL_S <= interval_s <= MAX_INTERVAL_S:
        raise PiObserverError("Observer interval_s is out of range")
    max_reviews = document.get("max_reviews",
                               agent_config.get("max_reviews",
                                                agent_config.get("max_turns",
                                                                 DEFAULT_MAX_REVIEWS)))
    if type(max_reviews) is not int or not 1 <= max_reviews <= MAX_REVIEWS:
        raise PiObserverError("Observer max_reviews is out of range")
    return PiObserverSpec(
        run_id=run_id, agent_id=environment.agent_id, role=role,
        state_root=state_root, workspace_root=workspace_root,
        reference_root=reference_root, pi_cli=pi_cli,
        pi_context_window=int(context_window),
        pi_session_max_bytes=pi_session_max_bytes,
        pi_artifact_max_bytes=pi_artifact_max_bytes,
        heartbeat_s=heartbeat_s, interval_s=interval_s,
        max_reviews=max_reviews, agent_config=agent_config, pi_model=pi_model,
    )


# ---------------------------------------------------------------------------
# Prompt appendix and Pi command preparation
# ---------------------------------------------------------------------------

def _observer_appendix(spec: PiObserverSpec) -> str:
    """Short environment note appended to the paper Observer prompt."""
    return "\n".join([
        "Runtime environment:",
        f"- Your workspace is {spec.workspace_root}; use read/write to keep review notes.",
        "- You are an independent witness: you do not play and you have no game client, "
        "shell, memory or web tools.",
        "- records_read returns experiment records page by page; cite an event by its "
        "event_seq. evidence_read reads one referenced artifact; cite its SHA256 hash.",
        "- observer_report publishes your advisory report: progress_summary, "
        "verified_events (claim and evidence_refs), infrastructure_findings and findings "
        "(verified, player_claim, insufficient_evidence, contradicted). Cite records or "
        "artifact hashes in evidence_refs.",
        "- infra_issue is only for failures of the environment; use kind reconnect, "
        "runtime_recovery, server_recovery, broken_tool or quota_ceiling. Never intervene "
        "in the players' strategy or game state.",
        "- Never invent evidence. If the record cannot establish a claim, say so.",
    ])


def _system_prompt(spec: PiObserverSpec) -> str:
    config = spec.agent_config
    if config.get("profile") == "paper_aligned":
        base = paper_observer_prompt(config)
    else:
        base = observer_prompt(config)
    return base.rstrip() + "\n\n" + _observer_appendix(spec)


def _extension_path(spec: PiObserverSpec) -> Path:
    for parent in spec.pi_cli.parents:
        if parent.name == "pi_runtime":
            candidate = parent / OBSERVER_EXTENSION_RELATIVE
            if candidate.is_file():
                return candidate
    fallback = _repo_root() / "pi_runtime" / OBSERVER_EXTENSION_RELATIVE
    if not fallback.is_file():
        raise PiObserverError(
            "the Pi observer tools extension is missing: " + str(fallback))
    return fallback


def _observer_command(spec: PiObserverSpec, *, prompt_path: Path, session_dir: Path,
                      node: str, resume: bool, extension_path: Path) -> list[str]:
    command = [
        node, str(spec.pi_cli),
        "--mode", "rpc",
        "--offline",
        "--provider", "mcproxy",
        "--model", str(spec.pi_model["model"]),
        "--tools", ",".join(OBSERVER_TOOLS),
        "--system-prompt", str(prompt_path),
        "--session-dir", str(session_dir),
        "--no-context-files",
        "--no-approve",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-mcp",
        "--extension", str(extension_path),
    ]
    if resume:
        command.append("--continue")
    return command


def _child_environment(*, home: Path, bin_dir: Path, tmp_dir: Path,
                       bridge_url: str) -> dict[str, str]:
    parent = os.environ
    environment: dict[str, str] = {}
    for key in _FORWARDED_CHILD_ENV_KEYS:
        value = parent.get(key)
        if isinstance(value, str) and value:
            environment[key] = value
    if "PYTHONPATH" not in environment:
        fallback = _repo_root() / "src"
        if fallback.is_dir():
            environment["PYTHONPATH"] = str(fallback)
    environment["PATH"] = str(bin_dir) + os.pathsep + environment.get(
        "PATH", "/usr/local/bin:/usr/bin:/bin")
    environment["HOME"] = str(home)
    environment["TMPDIR"] = str(tmp_dir)
    no_proxy = parent.get("NO_PROXY") or parent.get("no_proxy") or ""
    entries = [item.strip() for item in no_proxy.split(",") if item.strip()]
    for host in ("127.0.0.1", "localhost", "::1"):
        if host not in entries:
            entries.append(host)
    environment["NO_PROXY"] = ",".join(entries)
    environment["no_proxy"] = environment["NO_PROXY"]
    environment.update({
        "HARNESS_PI_BRIDGE_URL": bridge_url,
        "PI_CODING_AGENT_DIR": str(home / ".pi" / "agent"),
        "PI_OFFLINE": "1",
        "PI_SKIP_VERSION_CHECK": "1",
        "PI_TELEMETRY": "0",
    })
    return environment


@dataclass
class PiObserverPaths:
    home: Path
    agent_dir: Path
    session_dir: Path
    bin_dir: Path
    tmp_dir: Path
    models_path: Path
    system_prompt_path: Path
    log_path: Path


@dataclass
class PreparedPiObserver:
    environment: _pi.PiEnvironment
    spec: PiObserverSpec
    client: Any
    proxy: PiModelProxy
    bridge: PiObserverBridge
    artifact_budget: ArtifactBudget
    paths: PiObserverPaths
    system_prompt: str
    prompt_sha256: str
    tools_sha256: str
    command: list[str]
    child_env: dict[str, str]
    session_resume: bool
    session_rotated: bool
    ready_params: dict[str, Any]

    def append_event(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            self.client.call_sync("recorder.append", {
                "kind": kind, "payload": payload,
                "kwargs": {"actor_id": self.spec.agent_id}})
        except Exception:
            # Ledger evidence must not kill the review loop mid-run.
            return

    def put_artifact(self, value: Any, limit: int) -> dict[str, Any] | None:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
            encoded = text.encode("utf-8")
            if len(encoded) > limit:
                marker = "\n[pi observer runner: artifact truncated at its event limit]"
                text = encoded[:max(0, limit - len(marker))].decode(
                    "utf-8", errors="ignore") + marker
            from .pi_model_proxy import budgeted_artifact_text
            text = budgeted_artifact_text(self.artifact_budget, "tool_result", text)
            result = self.client.call_sync("recorder.put_artifact", {"text": text})
            return result if isinstance(result, dict) else None
        except Exception:
            return None


def prepare_observer(environment: _pi.PiEnvironment,
                     spec: PiObserverSpec) -> PreparedPiObserver:
    """Create local Pi state, start the proxy/bridge and build the Pi command."""
    paths = PiObserverPaths(
        home=spec.state_root / "home",
        agent_dir=spec.state_root / "home" / ".pi" / "agent",
        session_dir=spec.state_root / "pi-sessions",
        bin_dir=spec.state_root / "bin",
        tmp_dir=spec.state_root / "tmp",
        models_path=spec.state_root / "home" / ".pi" / "agent" / "models.json",
        system_prompt_path=spec.state_root / "system-prompt.txt",
        log_path=spec.state_root / "pi.log",
    )
    for directory in (spec.state_root, spec.workspace_root, paths.home,
                      paths.agent_dir, paths.session_dir, paths.bin_dir,
                      paths.tmp_dir):
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise PiObserverError(
                f"Could not create observer directory {directory}: "
                f"{exc.strerror or type(exc).__name__}") from None
    # Bounded session retention runs before the resume decision and before any
    # Pi process can append more session data (shared pi_runner policy).
    session_retention = _pi.apply_session_retention(
        paths.session_dir, max_bytes=spec.pi_session_max_bytes,
        state_root=spec.state_root)

    client = _pi.RpcClient(str(environment.socket_path), environment.token,
                           environment.agent_id, environment.role)
    _pi._record_session_retention(client, spec.agent_id, session_retention,
                                  max_bytes=spec.pi_session_max_bytes)

    def _record_artifact_retention(payload: dict[str, Any]) -> None:
        event_kind = payload.get("event", "pi_artifact_retention")
        body = {key: value for key, value in payload.items() if key != "event"}
        _pi._append_runner_event(client, spec.agent_id, event_kind,
                                 {"runtime": "pi-observer", **body})

    # Authoritative artifact accounting is the control plane's pi-budget.sqlite3
    # (outside every mount the Observer can write).
    artifact_budget = ArtifactBudget(
        spec.pi_artifact_max_bytes, event=_record_artifact_retention,
        state_path=spec.state_root / "pi-artifact-budget.json",
        rpc_client=client, agent_id=spec.agent_id)
    pi_cli_api_key = secrets.token_urlsafe(32)

    def _on_first_authenticated_request() -> None:
        _pi._scrub_pi_cli_auth_files(paths.agent_dir)
        _pi._append_runner_event(client, spec.agent_id, "pi_cli_auth_redacted", {
            "runtime": "pi-observer", "reason": "local_proxy_key_retired",
            "models_json_removed": True,
        })

    proxy = PiModelProxy(spec.pi_model, client, agent_id=spec.agent_id,
                         role="observer", purpose="observer_review",
                         artifact_budget=artifact_budget,
                         client_api_key=pi_cli_api_key,
                         on_client_authenticated=_on_first_authenticated_request)
    bridge: PiObserverBridge | None = None
    try:
        proxy_base_url = proxy.start()
        bridge = PiObserverBridge(client, agent_id=spec.agent_id,
                                  expected_agent_id=environment.agent_id)
        bridge_url = bridge.start()
        extension_path = _extension_path(spec)
        system_prompt = _system_prompt(spec)
        _pi._scrub_pi_cli_auth_files(paths.agent_dir)
        _pi._write_private(paths.models_path,
                           json.dumps(_pi._models_document(spec, proxy_base_url,
                                                           pi_cli_api_key),
                                      ensure_ascii=False, indent=2) + "\n")
        _pi._write_private(paths.system_prompt_path, system_prompt)
        prompt_sha256 = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
        tools_sha256 = hashlib.sha256(
            json.dumps({"tools": list(OBSERVER_TOOLS)},
                       sort_keys=True).encode("utf-8")).hexdigest()
        session_resume = session_retention.resume
        node = _pi._resolve_node()
        command = _observer_command(spec, prompt_path=paths.system_prompt_path,
                                    session_dir=paths.session_dir, node=node,
                                    resume=session_resume,
                                    extension_path=extension_path)
        child_env = _child_environment(home=paths.home, bin_dir=paths.bin_dir,
                                       tmp_dir=paths.tmp_dir,
                                       bridge_url=bridge_url)
    except BaseException:
        if bridge is not None:
            bridge.stop()
        proxy.stop()
        raise
    assert bridge is not None
    ready_params = {
        "agent_id": environment.agent_id,
        "role": environment.role,
        "pid": os.getpid(),
        "prompt_sha256": prompt_sha256,
        "tools_sha256": tools_sha256,
        "runtime": "pi-observer",
        "interval_s": spec.interval_s,
        "max_reviews": spec.max_reviews,
    }
    return PreparedPiObserver(
        environment=environment, spec=spec, client=client, proxy=proxy,
        bridge=bridge, artifact_budget=artifact_budget, paths=paths,
        system_prompt=system_prompt, prompt_sha256=prompt_sha256,
        tools_sha256=tools_sha256, command=command, child_env=child_env,
        session_resume=session_resume, session_rotated=session_retention.rotated,
        ready_params=ready_params)


# ---------------------------------------------------------------------------
# Ledger events and review trigger
# ---------------------------------------------------------------------------

def _turns(state: dict[str, Any]) -> int:
    value = state.get("rounds", 0)
    return value if type(value) is int and value >= 0 else 0


def _reviews(state: dict[str, Any]) -> int:
    value = state.get("reviews", 0)
    return value if type(value) is int and value >= 0 else 0


def _reports(state: dict[str, Any]) -> int:
    value = state.get("reports", 0)
    return value if type(value) is int and value >= 0 else 0


def _sha256_args(args: Any) -> str:
    encoded = json.dumps({"arguments": args}, ensure_ascii=False, sort_keys=True,
                         default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _handle_pi_event(prepared: PreparedPiObserver, record: dict[str, Any],
                     state: dict[str, Any]) -> None:
    record_type = record.get("type")
    if record_type == "agent_start":
        state["agent_runs"] = state.get("agent_runs", 0) + 1
        prepared.append_event("agent_started", {
            "runtime": "pi-observer", "round": _turns(state),
            "resumed": prepared.session_resume,
            "model": str(prepared.spec.pi_model.get("model", ""))[:200],
        })
    elif record_type == "tool_execution_start":
        tool = str(record.get("toolName", ""))[:100]
        prepared.append_event("tool_intent", {
            "tool": tool,
            "call_id": str(record.get("toolCallId", ""))[:200],
            "args_sha256": _sha256_args(record.get("args")),
            "idempotency": ("read_only" if tool in OBSERVER_READ_ONLY_TOOLS
                            else "side_effect"),
            "runtime": "pi-observer",
        })
    elif record_type == "tool_execution_end":
        result = record.get("result")
        reference = prepared.put_artifact(result, PI_OBSERVER_MAX_TOOL_ARTIFACT_BYTES)
        tool = str(record.get("toolName", ""))[:100]
        prepared.append_event("tool_result", {
            "tool": tool,
            "tool_call_id": str(record.get("toolCallId", ""))[:200],
            "is_error": bool(record.get("isError")),
            "duration_ms": record.get("durationMs"),
            "result_ref": reference,
            "runtime": "pi-observer",
        })
        if tool == "observer_report" and not record.get("isError"):
            state["reports"] = _reports(state) + 1
    elif record_type == "compaction_start":
        state["compaction_reason"] = record.get("reason")
    elif record_type == "compaction_end":
        result = record.get("result")
        reference = prepared.put_artifact(result,
                                          PI_OBSERVER_MAX_COMPACTION_ARTIFACT_BYTES)
        prepared.append_event("context_compacted", {
            "runtime": "pi-observer",
            "reason": state.get("compaction_reason") or record.get("reason"),
            "result_ref": reference,
        })
    elif record_type == "agent_settled":
        prepared.append_event("agent_turn", {
            "runtime": "pi-observer", "round": _turns(state),
            "reviews": _reviews(state), "reports": _reports(state),
            "aborted": bool(record.get("aborted")),
        })


def _event_list(page: Any) -> tuple[list[dict[str, Any]], int | None]:
    if isinstance(page, list):
        events = [event for event in page if isinstance(event, dict)]
        cursor = max((event.get("event_seq") for event in events
                      if type(event.get("event_seq")) is int), default=None)
        return events, cursor
    if isinstance(page, dict):
        events = [event for event in page.get("events") or []
                  if isinstance(event, dict)]
        cursor = page.get("next_cursor")
        return events, cursor if type(cursor) is int else None
    return [], None


def _important_event(event: dict[str, Any]) -> bool:
    if event.get("actor_id") == "observer":
        return False
    kind = event.get("kind")
    if kind in IMPORTANT_KINDS:
        return True
    if kind in {"player_event", "bot_event"}:
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("kind") in IMPORTANT_KINDS:
            return True
    return False


async def _wait_for_review_trigger(prepared: PreparedPiObserver,
                                   process: asyncio.subprocess.Process,
                                   stop_event: asyncio.Event,
                                   state: dict[str, Any]) -> str:
    """Wait for an important ledger event or the review interval.

    Returns ``important``, ``interval``, ``stop`` or ``exit``.  Ledger polling is
    bounded to at most once per second and always advances the stored cursor.
    """
    deadline = time.monotonic() + prepared.spec.interval_s
    cursor = state.get("event_cursor", 0)
    next_session_guard = time.monotonic() + SESSION_GUARD_SECONDS
    while True:
        if stop_event.is_set():
            return "stop"
        if process.returncode is not None:
            return "exit"
        try:
            page = await prepared.client.call(
                "recorder.read_events", {"after": cursor, "limit": 1000},
                timeout_s=30.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            page = []
        events, next_cursor = _event_list(page)
        if next_cursor is not None and next_cursor >= cursor:
            cursor = next_cursor
        state["event_cursor"] = cursor
        if any(_important_event(event) for event in events):
            return "important"
        now = time.monotonic()
        if now >= deadline:
            return "interval"
        if now >= next_session_guard:
            next_session_guard = now + SESSION_GUARD_SECONDS
            try:
                if _pi._check_active_session_limit(prepared, state, stop_event):
                    return "stop" if stop_event.is_set() else "exit"
            except Exception:
                pass
        delay = min(1.0, max(0.01, deadline - now))
        try:
            await asyncio.wait_for(stop_event.wait(), delay)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Signals, heartbeat, reporting
# ---------------------------------------------------------------------------

def _process_rss_bytes() -> int:
    return _pi._process_rss_bytes()


async def _heartbeat_loop(client: Any, state: dict[str, Any],
                          interval_s: float) -> None:
    timeout_s = max(_pi.HEARTBEAT_CALL_MIN_TIMEOUT_SECONDS,
                    min(_pi.HEARTBEAT_CALL_MAX_TIMEOUT_SECONDS, interval_s * 2))
    while True:
        await asyncio.sleep(interval_s)
        payload = {"turns": _turns(state), "reviews": _reviews(state),
                   "reports": _reports(state),
                   "status": state.get("status", "running"),
                   "rss_bytes": _process_rss_bytes(),
                   "runtime": "pi-observer"}
        try:
            await client.call("runner.heartbeat", payload, timeout_s=timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            continue


def _exit_result(prepared: PreparedPiObserver,
                 process: asyncio.subprocess.Process,
                 state: dict[str, Any], *, reason: str) -> dict[str, Any]:
    status = "stopped" if state.get("stop_requested") else None
    if status is None and prepared.proxy.denials > 0:
        status = "budget_exhausted"
    if status is None and prepared.proxy.upstream_errors > 0:
        status = "model_failed"
    if status is None:
        status = "infra_failed"
    return {"agent_id": prepared.spec.agent_id, "status": status,
            "turns": _turns(state), "reviews": _reviews(state),
            "reports": _reports(state), "reason": reason,
            "pi_returncode": process.returncode,
            "budget_denials": prepared.proxy.denials,
            "upstream_errors": prepared.proxy.upstream_errors}


def _stop_result(prepared: PreparedPiObserver, state: dict[str, Any],
                 reason: str) -> dict[str, Any]:
    return {"agent_id": prepared.spec.agent_id, "status": "stopped",
            "turns": _turns(state), "reviews": _reviews(state),
            "reports": _reports(state), "reason": reason}


async def _abort_and_wait(prepared: PreparedPiObserver,
                          process: asyncio.subprocess.Process,
                          queue: asyncio.Queue,
                          state: dict[str, Any], reason: str) -> dict[str, Any]:
    """Abort the active Pi review, wait briefly for Pi, then hard-stop it."""
    state["status"] = "stopping"
    stdin = process.stdin
    if stdin is not None:
        try:
            await _pi._send_json(stdin, {"type": "abort"})
        except (BrokenPipeError, ConnectionError, RuntimeError, OSError):
            pass
    deadline = time.monotonic() + PI_OBSERVER_ABORT_GRACE_SECONDS
    while time.monotonic() < deadline and process.returncode is None:
        remaining = deadline - time.monotonic()
        try:
            record = await asyncio.wait_for(queue.get(),
                                            timeout=min(0.25, max(0.01, remaining)))
        except asyncio.TimeoutError:
            continue
        if record is None:
            break
        if record.get("type") == "agent_settled":
            break
    await _pi._close_stdin(process)
    await _pi._terminate_process(process, PI_OBSERVER_TERMINATE_GRACE_SECONDS)
    prepared.append_event("agent_stopped", {
        "runtime": "pi-observer", "status": "stopped",
        "turns": _turns(state), "reviews": _reviews(state),
        "reports": _reports(state), "reason": reason,
    })
    return _stop_result(prepared, state, reason)


async def _finish_terminated(prepared: PreparedPiObserver,
                             process: asyncio.subprocess.Process,
                             state: dict[str, Any], result: dict[str, Any],
                             reason: str) -> dict[str, Any]:
    await _pi._close_stdin(process)
    await _pi._terminate_process(process, PI_OBSERVER_TERMINATE_GRACE_SECONDS)
    prepared.append_event("agent_stopped", {
        "runtime": "pi-observer", "status": result.get("status"),
        "turns": _turns(state), "reviews": _reviews(state),
        "reports": _reports(state), "reason": reason,
    })
    return result


async def _session_loop(prepared: PreparedPiObserver,
                        process: asyncio.subprocess.Process,
                        queue: asyncio.Queue,
                        stop_event: asyncio.Event,
                        stop_wait: asyncio.Task,
                        state: dict[str, Any]) -> dict[str, Any]:
    stdin = process.stdin
    if stdin is None:
        raise PiObserverError("Pi subprocess has no stdin pipe")
    prompt_counter = 0

    async def send_prompt(message: str) -> None:
        nonlocal prompt_counter
        prompt_counter += 1
        state["rounds"] = prompt_counter
        state["reviews"] = prompt_counter
        await _pi._send_json(stdin, {"id": f"review-{prompt_counter}",
                                     "type": "prompt", "message": message})

    await send_prompt(RESUME_PROMPT if prepared.session_resume else INITIAL_PROMPT)
    max_model_failures = int(
        prepared.spec.agent_config.get("max_model_failures", 5) or 5)
    max_model_failures = max(1, min(max_model_failures, 100))

    while True:
        if stop_event.is_set():
            state["stop_requested"] = True
            reason = "budget_stop" if state.get("budget_stopped") else "signal"
            return await _abort_and_wait(prepared, process, queue, state, reason)
        get_task = asyncio.create_task(queue.get())
        try:
            done, _pending = await asyncio.wait(
                {get_task, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            get_task.cancel()
            await asyncio.gather(get_task, return_exceptions=True)
            raise
        if get_task not in done:
            get_task.cancel()
            await asyncio.gather(get_task, return_exceptions=True)
            state["stop_requested"] = True
            reason = "budget_stop" if state.get("budget_stopped") else "signal"
            return await _abort_and_wait(prepared, process, queue, state, reason)
        record = get_task.result()
        if record is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                await _pi._terminate_process(process,
                                             PI_OBSERVER_TERMINATE_GRACE_SECONDS)
            result = _exit_result(prepared, process, state, reason="pi_exited")
            if result["status"] != "stopped":
                prepared.append_event("agent_error", {
                    "runtime": "pi-observer", "error": "pi_exited",
                    "returncode": process.returncode,
                })
            prepared.append_event("agent_stopped", {
                "runtime": "pi-observer", "status": result["status"],
                "turns": _turns(state), "reviews": _reviews(state),
                "reports": _reports(state), "reason": "pi_exited",
            })
            return result
        _handle_pi_event(prepared, record, state)
        if record.get("type") == "response" and record.get("command") == "prompt" \
                and record.get("success") is False:
            prepared.append_event("agent_error", {
                "runtime": "pi-observer", "error": "prompt_rejected",
                "message": str(record.get("error", ""))[:500],
            })
            result = _exit_result(prepared, process, state,
                                  reason="prompt_rejected")
            return await _finish_terminated(prepared, process, state, result,
                                            "prompt_rejected")
        if record.get("type") != "agent_settled":
            continue
        # One review settled: decide whether another review is due.
        if process.returncode is not None:
            continue  # the EOF branch classifies the exit
        try:
            stopped = await prepared.client.call("budget.stop_requested")
        except asyncio.CancelledError:
            raise
        except Exception:
            stopped = False
        if stopped:
            state["stop_requested"] = True
            return await _abort_and_wait(prepared, process, queue, state,
                                         "budget_stop")
        if prepared.proxy.denials > 0:
            result = {"agent_id": prepared.spec.agent_id,
                      "status": "budget_exhausted", "turns": _turns(state),
                      "reviews": _reviews(state), "reports": _reports(state),
                      "reason": "budget_denied",
                      "budget_denials": prepared.proxy.denials}
            return await _finish_terminated(prepared, process, state, result,
                                            "budget_denied")
        if (prepared.proxy.fatal_upstream_error
                or prepared.proxy.consecutive_upstream_errors >= max_model_failures):
            prepared.append_event("agent_error", {
                "runtime": "pi-observer", "error": "pi_upstream_errors",
                "upstream_errors": prepared.proxy.upstream_errors,
                "last_upstream_status": prepared.proxy.last_upstream_status,
                "fatal": bool(prepared.proxy.fatal_upstream_error),
            })
            result = {"agent_id": prepared.spec.agent_id,
                      "status": "model_failed", "turns": _turns(state),
                      "reviews": _reviews(state), "reports": _reports(state),
                      "reason": "pi_upstream_errors",
                      "upstream_errors": prepared.proxy.upstream_errors}
            return await _finish_terminated(prepared, process, state, result,
                                            "pi_upstream_errors")
        if prompt_counter >= prepared.spec.max_reviews:
            result = {"agent_id": prepared.spec.agent_id,
                      "status": "turns_exhausted", "turns": _turns(state),
                      "reviews": _reviews(state), "reports": _reports(state),
                      "reason": "max_reviews"}
            return await _finish_terminated(prepared, process, state, result,
                                            "max_reviews")
        trigger = await _wait_for_review_trigger(prepared, process, stop_event, state)
        if trigger == "stop":
            state["stop_requested"] = True
            reason = "budget_stop" if state.get("budget_stopped") else "signal"
            return await _abort_and_wait(prepared, process, queue, state, reason)
        if trigger == "exit":
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                await _pi._terminate_process(
                    process, PI_OBSERVER_TERMINATE_GRACE_SECONDS)
            result = _exit_result(prepared, process, state, reason="pi_exited")
            prepared.append_event("agent_stopped", {
                "runtime": "pi-observer", "status": result["status"],
                "turns": _turns(state), "reviews": _reviews(state),
                "reports": _reports(state), "reason": "pi_exited",
            })
            return result
        await send_prompt(REVIEW_PROMPT)


# ---------------------------------------------------------------------------
# Process driving
# ---------------------------------------------------------------------------

async def _run_prepared(prepared: PreparedPiObserver,
                        early_stop: threading.Event,
                        real_stdout: Any) -> int:
    stop_event = asyncio.Event()
    _pi._install_stop_signal_handlers(stop_event)
    if early_stop.is_set():
        return 0
    try:
        await prepared.client.call("runner.ready", prepared.ready_params,
                                   timeout_s=PI_OBSERVER_READY_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if stop_event.is_set() or early_stop.is_set():
            return 0
        raise PiObserverError(
            f"runner.ready failed: {type(exc).__name__}") from None
    if stop_event.is_set():
        await _pi._deliver_finished(
            prepared.client,
            _stop_result(prepared, {"rounds": 0}, "signal"), real_stdout)
        return 0

    state: dict[str, Any] = {"status": "running", "rounds": 0,
                             "reviews": 0, "reports": 0, "event_cursor": 0}
    log_handle = open(prepared.paths.log_path, "ab")
    process: asyncio.subprocess.Process | None = None
    heartbeat_task: asyncio.Task | None = None
    budget_task: asyncio.Task | None = None
    stop_wait: asyncio.Task | None = None
    pump_task: asyncio.Task | None = None
    try:
        bridge_fd = _pi._create_bridge_token_fd(prepared.bridge.token)
        spawn_env = dict(prepared.child_env)
        spawn_env[_pi.ENV_PI_BRIDGE_TOKEN_FD] = str(bridge_fd)
        try:
            process = await asyncio.create_subprocess_exec(
                *prepared.command, cwd=str(prepared.spec.workspace_root),
                env=spawn_env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=log_handle,
                limit=PI_OBSERVER_STREAM_LIMIT, start_new_session=True,
                pass_fds=(bridge_fd,))
        except OSError as exc:
            raise PiObserverError(
                f"Could not start the Pi CLI: {exc.strerror or type(exc).__name__}") from None
        finally:
            try:
                os.close(bridge_fd)
            except OSError:
                pass
        queue: asyncio.Queue = asyncio.Queue()
        pump_task = asyncio.create_task(_pi._pump_stdout(process.stdout, queue),
                                        name="pi-observer-stdout")
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(prepared.client, state, prepared.spec.heartbeat_s),
            name="pi-observer-heartbeat")
        budget_task = asyncio.create_task(
            _pi._budget_watch_loop(prepared.client, stop_event, state),
            name="pi-observer-budget-watch")
        stop_wait = asyncio.create_task(stop_event.wait(),
                                        name="pi-observer-stop")
        try:
            return_value = await _session_loop(prepared, process, queue,
                                               stop_event, stop_wait, state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return_value = {"agent_id": prepared.spec.agent_id,
                            "status": "infra_failed", "turns": _turns(state),
                            "reviews": _reviews(state), "reports": _reports(state),
                            "error": type(exc).__name__}
            prepared.append_event("agent_error", {
                "runtime": "pi-observer", "error": type(exc).__name__})
            prepared.append_event("agent_stopped", {
                "runtime": "pi-observer", "status": "infra_failed",
                "turns": _turns(state), "reviews": _reviews(state),
                "reports": _reports(state)})
        state["status"] = return_value.get("status", "infra_failed")
        await _pi._deliver_finished(prepared.client, return_value, real_stdout)
        return 0
    finally:
        for task in (pump_task, heartbeat_task, budget_task, stop_wait):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*[task for task in
                               (pump_task, heartbeat_task, budget_task, stop_wait)
                               if task is not None], return_exceptions=True)
        if process is not None:
            try:
                await _pi._close_stdin(process)
                await _pi._terminate_process(process, grace=2.0)
            except Exception:
                pass
        try:
            _pi.apply_session_retention(
                prepared.paths.session_dir, max_bytes=prepared.spec.pi_session_max_bytes,
                state_root=prepared.spec.state_root)
        except Exception:
            pass
        try:
            prepared.artifact_budget.flush()
        except Exception:
            pass
        try:
            _pi._scrub_pi_cli_auth_files(prepared.paths.agent_dir)
        except Exception:
            pass
        try:
            log_handle.close()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    """Process entry point: ``python -m minecraft_harness.pi_observer_runner``."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    real_stdout = sys.__stdout__ if sys.__stdout__ is not None else sys.stdout
    # Only the final JSON fallback may ever reach stdout; Pi's protocol is on
    # its own pipe, never this process' stdout.
    sys.stdout = sys.stderr
    try:
        _pi.harden_process_credentials()
    except _pi.CredentialHygieneError as exc:
        _pi._write_startup_error(exc)
        return 2
    if arguments:
        _pi._write_startup_error(
            PiObserverError("The Pi observer runner entry point takes no arguments"))
        return 2
    with _pi._EarlySignalGuard() as guard:
        prepared: PreparedPiObserver | None = None
        try:
            environment = _pi.PiEnvironment.from_environment(os.environ)
            spec = load_spec(environment)
            prepared = prepare_observer(environment, spec)
        except Exception as exc:
            _pi._write_startup_error(exc)
            return 2
        if guard.stop_event.is_set():
            prepared.bridge.stop()
            prepared.proxy.stop()
            return 0
        try:
            return asyncio.run(_run_prepared(prepared, guard.stop_event,
                                             real_stdout))
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            _pi._write_startup_error(exc)
            return 2
        finally:
            try:
                prepared.bridge.stop()
            except Exception:
                pass
            try:
                prepared.proxy.stop()
            except Exception:
                pass
            try:
                _pi._scrub_pi_cli_auth_files(prepared.paths.agent_dir)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
