"""Experiment lifecycle; never allocates game strategies or player roles."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import importlib.metadata
import os
import platform
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .agents import AgentLoop
from .budget import Budget, BudgetExhausted
from .config import validate_config
from .evaluation import ServerEvidenceCollector, IncrementalEvaluator, evaluate
from .execution import ExecutionGateway
from .execution.gateway import RuntimeReliabilityError, RuntimeUnavailable
from .memory import MemoryStore
from .models import build_model
from .observer import write_report
from .ownership import RuntimeOwnership
from .environment import inspect_environment, capture_deployment
from .workspaces import WorkspaceProvisioner
from .recorder import Recorder, StorageLimitError, atomic_json, enqueue_control
from .server import ServerControl
from .world_checkpoints import create_checkpoint, restore_checkpoint, recover_pending_restore, has_interrupted_restore, read_pending_restore_receipt, acknowledge_restore_receipt


def _git_sha():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def is_active(run_dir):
    path = Path(run_dir) / ".supervisor.lock"
    if not path.exists():
        return False
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def request_control(run_dir, command, **arguments):
    if command not in {"pause", "freeze", "resume", "stop", "checkpoint", "restore"}:
        raise ValueError("unknown control command")
    path = Path(run_dir)
    if not is_active(path):
        raise RuntimeError("no live supervisor owns this run; use resume to restart from checkpoints")
    return enqueue_control(path, command, arguments)


class Supervisor:
    def __init__(self, config, run_dir, resume=False):
        self.config = validate_config(config)
        self.run_dir = Path(run_dir).resolve()
        self.resume = resume
        self.gateway = self.recorder = self.budget = None
        self.status = "starting"
        self.frozen = False
        self.sessions_fenced = False
        self._observer_outcome_recorded = False

    def _manifest(self):
        encoded = json.dumps(self.config, sort_keys=True).encode()
        root = Path(__file__).resolve().parents[2]
        refs = {}
        files = list((root / "src").rglob("*.py")) + list((root / "bot_runtime" / "src").glob("*.js"))
        files += [root / "uv.lock", root / "bot_runtime" / "package-lock.json"]
        for path in sorted(files):
            if path.is_file():
                refs[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return {"schema_version": 1, "run_id": uuid.uuid4().hex, "harness_version": __version__, "git_sha": _git_sha(), "python": platform.python_version(), "created_at": datetime.now(timezone.utc).isoformat(), "config": self.config, "config_sha256": hashlib.sha256(encoded).hexdigest(), "source_hashes": refs, "python_dependencies": {name: importlib.metadata.version(name) for name in ("httpx", "PyYAML")}, "runtime_mode": self.config["runtime"]["mode"], "model_kind": self.config["model"]["kind"], "diagnostic_only": self.config["runtime"]["mode"] == "fake" or self.config["model"]["kind"] == "fake", "background_jobs": self.config['runtime']['lifecycle'] == 'paper_aligned', "world_seed_operator_only": self.config["experiment"].get("seed")}

    async def _log_guard(self, collector):
        path = self.config['server'].get('log_path')
        while True:
            if path and Path(path).exists():
                await self._copy_safely(collector.collect_log, path, world_epoch=self.gateway.world_epoch,
                                       source_timezone=self.config['server']['log_timezone'])
            await asyncio.sleep(self.config['server']['log_interval_s'])

    async def _observer_infra_issue(self, issue):
        """A model report can request a probe, never authorize a game mutation."""
        key = 'observer_issue:' + hashlib.sha256(str(issue.get('issue_id', '')).encode()).hexdigest()
        if self.recorder.get_state(key):
            return {'status': 'duplicate'}
        kind = issue.get('kind')
        refs = issue.get('evidence_refs', [])
        valid_refs = []
        for reference in refs[:32]:
            sequence = reference.get('event_seq') if isinstance(reference, dict) else reference
            if type(sequence) is not int or sequence < 1:
                continue
            rows = self.recorder.read_events(sequence - 1, 1)
            if rows and rows[0]['event_seq'] == sequence and rows[0]['source'] not in {'agent_claim', 'orphaned_runtime'}:
                valid_refs.append(sequence)
        result = {'status': 'reported', 'reason': 'no independently confirmed infrastructure failure'}
        if (self.config['observer']['allow_infra_requests'] and valid_refs and
                kind in {'runtime_unavailable', 'server_unavailable', 'reconnect', 'runtime_recovery', 'server_recovery'} and self.status == 'running'):
            try:
                if kind in {'server_unavailable', 'server_recovery'}:
                    if not self._observer_server.container:
                        raise ValueError('no managed server to probe')
                    await self._observer_server.ready()
                else:
                    for agent in self.config['agents']['ids']:
                        await self.gateway._request('GET', '/health', agent_id=agent)
            except ValueError:
                pass
            except Exception:
                self.gateway.fault = RuntimeUnavailable('Observer request independently confirmed by Supervisor probe')
                result = {'status': 'recovery_requested', 'evidence_refs': valid_refs}
        self.recorder.state_event(key, result, 'observer_infra_issue_checked', {'issue': issue, 'result': result})
        return result

    def _consume_observer_outcome(self, outcome, *, expected_shutdown=False):
        """Persist reviewer health separately from game capability evaluation."""
        if self._observer_outcome_recorded:
            return False
        self._observer_outcome_recorded = True
        outcome = outcome if isinstance(outcome, dict) else {'status': 'infra_failed', 'reason': 'invalid Observer outcome'}
        status = outcome.get('status', 'infra_failed')
        required = self.config['experiment']['profile'] == 'paper_aligned'
        failure = status in {'model_failed', 'infra_failed', 'reliability_failed', 'resource_exhausted'}
        unexpected = not expected_shutdown and status not in {'budget_exhausted'} and not failure
        payload = {**outcome, 'required': required, 'expected_shutdown': expected_shutdown}
        try:
            self.recorder.state_event('observer_outcome', payload, 'observer_outcome', payload, actor_id='observer')
            if not required and (failure or unexpected):
                self.recorder.state_event('observer_degraded', True, 'observer_degraded', payload, actor_id='observer')
        except Exception as exc:
            self.status = 'infra_failed'
            self._record_error('observer_outcome_record_failed', exc)
            return True
        if status == 'budget_exhausted':
            self.status = 'budget_exhausted'
            return True
        if required and (failure or unexpected):
            self.status = 'model_failed' if status == 'model_failed' else 'infra_failed'
            return True
        return False

    async def _collect(self, collector, server, baseline=False, flush=True):
        if flush:
            try:
                await server.save()
            except RuntimeError as exc:
                raise RuntimeUnavailable("world save unavailable: " + str(exc)[:1000]) from exc
        world = self.config["server"].get("world_dir")
        if world:
            player_ids = [str(uuid.UUID(bytes=hashlib.md5(("OfflinePlayer:" + agent).encode()).digest(), version=3)) for agent in self.config["agents"]["ids"]]
            await self._copy_safely(collector.collect_world, world, world_epoch=self.gateway.world_epoch, baseline=baseline, player_ids=player_ids)
        await self._collect_logs(collector)

    async def _collect_logs(self,collector,*,final=False):
        log=self.config['server'].get('log_path')
        if log and Path(log).exists():
            deadline=time.monotonic()+self.config['server']['control_timeout_s']
            while True:
                await self._copy_safely(collector.collect_log, log, world_epoch=self.gateway.world_epoch,
                                       source_timezone=self.config['server']['log_timezone'],final=final)
                if not final:break
                status=await self._copy_safely(collector.log_status,log)
                if status.get('pending_bytes')==0:break
                if time.monotonic()>=deadline:
                    raise RuntimeError('final server log collection deadline exceeded with unarchived bytes')
        elif final and log:
            self.recorder.append('server_log_unavailable',{'path':log,'final':True})

    def _evaluation(self, final=False):
        result = evaluate(self.run_dir, self.config["experiment"]["task"]) if final else self.evaluator.evaluate()
        if (self.config["runtime"]["mode"] == "fake" or self.config["model"]["kind"] == "fake") and result.get("capability_success"):
            result = {**result, "status": "diagnostic_pass", "capability_success": None, "diagnostic_only": True}
        return result

    def _record_error(self, kind, exc):
        try:
            self.recorder.append(kind, {"type": type(exc).__name__, "message": str(exc)[:4000]})
        except Exception:
            try:
                atomic_json(self.run_dir / "emergency.json", {"status": "infra_failed", "kind": kind, "error": str(exc)[:1000], "pid": os.getpid()})
            except OSError:
                pass

    async def _resource_guard(self):
        import shutil
        next_sample = 0
        last_full_scan = -300.0
        while True:
            now = time.monotonic()
            full_scan = now - last_full_scan >= 300
            if full_scan:
                last_full_scan = now
            status = await self._copy_safely(self.recorder.storage_status, full=full_scan)
            resources = {'python_rss_bytes': None, 'sampled_at': datetime.now(timezone.utc).isoformat()}
            try:
                memory = next(line for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('VmRSS:'))
                resources = {'python_rss_bytes': int(memory.split()[1]) * 1024, 'sampled_at': datetime.now(timezone.utc).isoformat()}
                await self._copy_safely(self.recorder.set_state, 'resources', resources)
            except (OSError, StopIteration):
                pass
            world = self.config["server"].get("world_dir")
            if world:
                path = Path(world)
                while not path.exists() and path != path.parent:
                    path = path.parent
                status["world_free_bytes"] = shutil.disk_usage(path).free
                if status["world_free_bytes"] < self.config.get("storage", {}).get("min_free_disk_bytes", 268435456):
                    status["limit_exceeded"] = True
            await self._copy_safely(self.recorder.set_state, "storage", status)
            if status["limit_exceeded"]:
                raise StorageLimitError("run quota or filesystem free-space reserve exceeded")
            now = time.monotonic()
            if now >= next_sample:
                budget = self.budget.state if self.budget else {}
                sample = {**resources, 'storage': status,
                    'budget': {key: budget.get(key) for key in ('calls', 'input_tokens', 'output_tokens', 'cost_usd', 'unknown_usage_calls')},
                    'source': 'supervisor', 'measurement': 'supervisor_python_rss_not_worker_or_server_memory'}
                await self._copy_safely(self.recorder.append, 'resource_sample', sample,
                    world_epoch=self.gateway.world_epoch if self.gateway else 0)
                next_sample = now + self.config.get('supervision', {}).get('resource_sample_interval_s', 10)
            await asyncio.sleep(self.config.get("supervision", {}).get("resource_check_interval_s", 5))

    async def _recover_runtime(self, server, collector):
        settings = self.config.get("supervision", {})
        recoveries = self.recorder.get_state("runtime_recoveries", 0)
        if recoveries >= settings.get("max_recoveries", 20):
            raise RuntimeUnavailable("total runtime recovery limit exhausted")
        self.recorder.set_state("runtime_recoveries", recoveries + 1)
        self.budget.admission.clear()
        self.gateway.admission.clear()
        self.status = "recovering"
        self.recorder.set_state("status", self.status)
        last = None
        for attempt in range(settings.get("recovery_attempts", 3)):
            try:
                self.recorder.append("runtime_recovery_started", {"attempt": attempt + 1, "reason": str(self.gateway.fault)[:1000]})
                # The managed server may rotate latest.log on restart. Drain the
                # pre-crash tail before stopping/starting it so recovery does not
                # manufacture a server_log_gap and lose authoritative chat/deaths.
                try:
                    await self._collect_logs(collector)
                except Exception as exc:
                    self._record_error("pre_recovery_log_collection_failed", exc)
                if server.container:
                    await self.gateway.fence()
                    await self._reconcile_world(server)
                    await server.start()
                    await self.gateway.reset_world(already_fenced=True, paused=True)
                else:
                    await self.gateway.recover(paused=True)
                await self._collect(collector, server)
                await self.gateway.resume_sessions()
                self.gateway.admission.set()
                self.budget.admission.set()
                self.status = "running"
                self.recorder.set_state("status", self.status)
                self.recorder.append("runtime_recovery_completed", {"attempt": attempt + 1}, world_epoch=self.gateway.world_epoch)
                return
            except RuntimeReliabilityError:
                raise
            except Exception as exc:
                last = exc
                self._record_error("runtime_recovery_failed", exc)
                if attempt + 1 < settings.get("recovery_attempts", 3):
                    await asyncio.sleep(min(settings.get("recovery_max_s", 30), settings.get("recovery_base_s", 1) * 2 ** attempt))
        raise RuntimeUnavailable("bounded runtime recovery exhausted") from last

    async def run(self):
        self.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = (self.run_dir / ".supervisor.lock").open("a")
        ownership = RuntimeOwnership(self.config["runtime"], self.config["server"])
        tasks, models, outcomes = [], [], []
        memory = guard = operation = log_guard = observer_task = observer = workspaces = None
        server = collector = None
        paths, result = {}, {"status": "not_succeeded", "capability_success": False}
        already_elapsed, start = 0, time.monotonic()
        owned = runtime_started = config_migrated = False
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("this run already has a live supervisor") from None
            manifest_path = self.run_dir / "manifest.json"
            if manifest_path.exists() and not self.resume:
                raise ValueError("run directory already exists; resume it or choose a new directory")
            if self.resume:
                if not manifest_path.exists():
                    raise ValueError("cannot resume a directory without manifest.json")
                saved_config = json.loads(manifest_path.read_text())["config"]
                if validate_config(saved_config) != self.config:
                    raise ValueError("resumed run configuration must match its frozen manifest")
                config_migrated = saved_config != self.config
            else:
                atomic_json(manifest_path, self._manifest())
            self.recorder = Recorder(self.run_dir, limits=self.config.get("storage"))
            if config_migrated:
                self.recorder.append("config_defaults_migrated", {"reason": "new bounded runtime defaults added; original frozen values retained", "wall_deadline_origin": "manifest.created_at if no persisted deadline"})
            self.recorder.interrupt_controls()
            self.recorder.set_state("supervisor_pid", os.getpid())
            self.recorder.set_state("status", "starting")
            already_elapsed = self.recorder.get_state("active_wall_seconds", 0)
            self.budget = Budget(self.config["budgets"], self.recorder, max_output_tokens=self.config["model"]["max_output_tokens"])
            agents = self.config["agents"]["ids"]
            self.gateway = ExecutionGateway(self.config["runtime"], self.recorder, agents, self.run_dir)
            server = ServerControl(self.config["server"], self.recorder)
            self._observer_server = server
            collector = ServerEvidenceCollector(self.run_dir, self.recorder)
            self.evaluator = IncrementalEvaluator(self.run_dir, self.config["experiment"]["task"])
            ownership.acquire()
            owned = True
            memory = MemoryStore(self.run_dir / "memory", self.recorder, shared_enabled=self.config["collaboration"]["shared_memory"])
            if self.config["runtime"].get("url") or self.config["runtime"].get("urls"):
                await self.gateway.fence()
            if self.budget.stop_requested():
                raise BudgetExhausted("saved wall-time deadline has expired; no new sessions started")
            world = self.config["server"].get("world_dir")
            if world:
                await self._reconcile_world(server)
            if self.resume and server.container:
                await server.start()
            environment = await self._copy_safely(inspect_environment, self.config, run_id=self.recorder.run_id, resume=self.resume)
            self.recorder.set_state('effective_environment', environment)
            self.recorder.append('effective_environment_verified', environment)
            manifest = json.loads(manifest_path.read_text())
            manifest['effective_environment'] = environment
            atomic_json(manifest_path, manifest)
            self.gateway.config['remaining_run_ms'] = max(1, int((self.budget.deadline - time.monotonic()) * 1000))
            workspaces = WorkspaceProvisioner(self.config['workspace'], self.run_dir, run_id=self.recorder.run_id)
            coding_containers = await workspaces.provision(agents + (['observer'] if self.config['observer']['mode'] == 'llm' else []))
            if coding_containers:
                self.gateway.config['cli_broker_socket'] = str(self.run_dir / 'broker' / 'socket')
                self.gateway.config['cli_broker_socket_advertise'] = '/run/mc-broker/socket'
            source_epoch = self.gateway.world_epoch
            self.gateway.world_epoch += int(self.resume)
            self.recorder.set_state("world_epoch", self.gateway.world_epoch)
            if self.resume:
                lineage = {"source_world_epoch": source_epoch}
                self.recorder.append("world_restored", {"requires_reobservation": True, "globals_lost": True, "rollback": False, **lineage}, world_epoch=self.gateway.world_epoch)
                self.gateway.announce_restored(**lineage)
            guard = asyncio.create_task(self._resource_guard(), name="storage-guard")
            await self.gateway.start(paused=True)
            runtime_started = True
            runtime_health = {}
            for agent in agents:
                runtime_health[agent] = await self.gateway._request('GET', '/health', agent_id=agent)
            environment['runtime_health'] = runtime_health
            environment['deployment'] = await capture_deployment(self.config, dict(self.gateway.sessions), coding_containers, run_id=self.recorder.run_id)
            self.recorder.append('deployment_provenance_verified', environment['deployment'])
            if not self.config['web']['enabled'] or not self.config['web'].get('search_url'):
                environment.setdefault('deviations', []).append('web_search_unconfigured')
            if self.config['observer']['mode'] != 'llm':
                environment.setdefault('deviations', []).append('rules_observer')
            self.recorder.set_state('effective_environment', environment)
            manifest['effective_environment'] = environment
            manifest['agent_interfaces'] = {}
            atomic_json(manifest_path, manifest)  # Deployment evidence is durable before model construction.
            await self._collect(collector, server, baseline=not self.resume)
            await self.gateway.resume_sessions()
            self.recorder.set_state("world_frozen", False)
            self.recorder.append("run_resumed" if self.resume else "run_started", {"agents": agents, "mode": self.config["runtime"]["mode"]}, world_epoch=self.gateway.world_epoch)
            log_guard = asyncio.create_task(self._log_guard(collector), name='server-log-collector')
            for agent in agents:
                model = build_model(self.config["model"])
                models.append(model)
                agent_config = {**self.config["agents"], "agent_id": agent, "peers": [x for x in agents if x != agent], "goal": self.config["experiment"]["goal"],
                    'profile': self.config['experiment']['profile'], 'lifecycle': self.config['runtime']['lifecycle'], 'web': self.config['web'],
                    "game_chat": self.config["collaboration"]["game_chat"], "chat_delivery": self.config["collaboration"].get("chat_delivery", "events"), "model_request_timeout_s": self.config["agents"].get("model_request_timeout_s", self.config["model"]["timeout_s"] + 5),
                    "program_timeout_ms": int(self.config["runtime"]["program_timeout_default_s"] * 1000),
                    "leftover_policy": self.config["runtime"].get("leftover_policy", "reset"), "paper_tight_interface": self.config["agents"].get("paper_tight_interface", False)}
                if agent in coding_containers:
                    agent_config.update(shell_enabled=True, shell_container=coding_containers[agent], shell_env=self.gateway.cli_environment(agent))
                loop = AgentLoop(agent_config, self.run_dir, self.recorder, self.gateway, model, self.budget, memory)
                manifest['agent_interfaces'][agent] = {'prompt_sha256': hashlib.sha256(loop.messages[0]['content'].encode()).hexdigest(),
                    'tools_sha256': hashlib.sha256(json.dumps(loop.schemas, sort_keys=True).encode()).hexdigest()}
                tasks.append(asyncio.create_task(loop.run(), name=agent))
            if self.config['observer']['mode'] == 'llm':
                from .observer.agent import ObserverAgent
                observer_model = build_model(self.config['model'])
                models.append(observer_model)
                observer_config = {**self.config['agents'], **self.config['observer'], 'web': self.config['web'], 'lifecycle': self.config['runtime']['lifecycle']}
                if 'observer' in coding_containers:
                    observer_config.update(shell_enabled=True, shell_container=coding_containers['observer'])
                observer = ObserverAgent(observer_config, self.run_dir, self.recorder, observer_model, self.budget, infra_callback=self._observer_infra_issue)
                manifest['agent_interfaces']['observer'] = {'prompt_sha256': hashlib.sha256(observer.messages[0]['content'].encode()).hexdigest(),
                    'tools_sha256': hashlib.sha256(json.dumps(observer.schemas, sort_keys=True).encode()).hexdigest()}
                observer_task = asyncio.create_task(observer.run(), name='observer')
                self.recorder.set_state('observer_outcome', None)
                self.recorder.state_event('observer_degraded', False, 'observer_started', {'mode': 'llm', 'required': self.config['experiment']['profile'] == 'paper_aligned'})
            atomic_json(manifest_path, manifest)
            self.status = "running"
            self.recorder.set_state("status", self.status)
            settings = self.config.get("supervision", {})
            next_save = time.monotonic() + self.config["server"]["save_interval_s"]
            next_eval = next_heartbeat = time.monotonic()
            next_server_check = time.monotonic() + 30
            while not all(task.done() for task in tasks):
                now = time.monotonic()
                if guard.done():
                    guard.result()
                if log_guard.done():
                    log_guard.result()
                if observer_task and observer_task.done() and not self._observer_outcome_recorded:
                    if observer_task.cancelled():
                        observer_outcome = {'status': 'interrupted', 'reason': 'Observer unexpectedly cancelled'}
                    elif observer_task.exception():
                        observer_outcome = {'status': 'infra_failed', 'error': type(observer_task.exception()).__name__}
                    else:
                        observer_outcome = observer_task.result()
                    if self._consume_observer_outcome(observer_outcome):
                        break
                if operation and operation.done():
                    try:
                        operation.result()
                    except RuntimeUnavailable as exc:
                        self.gateway.fault = exc
                    operation = None
                if now >= next_heartbeat:
                    await asyncio.to_thread(self.recorder.set_state, "active_wall_seconds", already_elapsed + now - start)
                    next_heartbeat = now + 1
                control = await asyncio.to_thread(self.recorder.claim_control)
                if control:
                    if control["command"] == "stop":
                        self.budget.stopped = True
                        self.recorder.finish_control(control["control_id"], "applied", {"status": "stopped"})
                    elif operation:
                        self.recorder.finish_control(control["control_id"], "failed", {"error": "maintenance operation already in progress"})
                    else:
                        operation = asyncio.create_task(self._apply_control(control, server, collector), name="operator-maintenance")
                if self.budget.stop_requested():
                    self.status = "stopped" if self.budget.stopped else "budget_exhausted"
                    break
                if not operation and not self.frozen and self.gateway.fault:
                    if isinstance(self.gateway.fault, RuntimeReliabilityError):
                        raise self.gateway.fault
                    operation = asyncio.create_task(self._recover_runtime(server, collector), name="runtime-recovery")
                if not operation and not self.frozen and server.container and now >= next_server_check:
                    async def check_server():
                        try:
                            await server.ready()
                        except Exception as exc:
                            self.gateway.fault = RuntimeUnavailable(f"server readiness failed: {exc}")
                    operation = asyncio.create_task(check_server(), name="server-health")
                    next_server_check = now + 30
                if not operation and not self.frozen and now >= next_save:
                    operation = asyncio.create_task(self._collect(collector, server), name="periodic-evidence")
                    next_save = now + self.config["server"]["save_interval_s"]
                if now >= next_eval:
                    result = await asyncio.to_thread(self._evaluation)
                    self.recorder.set_state('evaluation', result)
                    next_eval = now + settings.get("evaluation_interval_s", 2)
                    if result["status"] == "succeeded":
                        if not self.recorder.get_state('first_success'):
                            self.recorder.set_state('first_success', result)
                            self.recorder.append('first_success_verified', result)
                            if observer:
                                observer.wakeup({'kind': 'success_candidate', 'evaluation': result})
                        if self.config['experiment']['stop_on_success']:
                            self.status = "succeeded"
                            break
                if any(task.done() and (task.cancelled() or task.exception() or (isinstance(task.result(), dict) and task.result().get("status") in {"infra_failed", "model_failed"})) for task in tasks):
                    break
                await asyncio.sleep(settings.get("poll_interval_s", .2))
            if self.status == "running":
                self.status = "completed"
        except asyncio.CancelledError:
            self.status = "stopped"
        except Exception as exc:
            if self.recorder is None:
                raise
            self.status = "budget_exhausted" if isinstance(exc, BudgetExhausted) else "resource_exhausted" if isinstance(exc, StorageLimitError) else "reliability_failed" if isinstance(exc, RuntimeReliabilityError) else "infra_failed"
            self._record_error("infrastructure_error", exc)
        finally:
            try:
                if self.budget:
                    self.budget.stopped = True
                    self.budget.admission.set()
                if self.gateway:
                    self.gateway.admission.clear()
                if observer:
                    observer.stop()
                # Deliberate endings get a bounded grace period for in-flight model
                # calls to settle. Cancelling immediately recorded them as unknown
                # usage and made the final token/cost totals null even after a win.
                shutdown_grace = float(self.config.get("supervision", {}).get("shutdown_timeout_s", 30))
                deliberate = self.status in {"succeeded", "stopped", "budget_exhausted", "completed"}
                if deliberate and observer_task and not observer_task.done():
                    await asyncio.wait({observer_task}, timeout=shutdown_grace)
                if observer_task:
                    if not observer_task.done():
                        observer_task.cancel()
                    await asyncio.gather(observer_task, return_exceptions=True)
                    if not self._observer_outcome_recorded:
                        # A natural Observer exit during a deliberate shutdown
                        # (operator stop, wall deadline, success) is expected;
                        # only an exit while the run should continue is a fault.
                        # The soak race: the Observer sees budget.stop_requested()
                        # and exits by itself before this cancellation, which used
                        # to be misclassified as unexpected -> infra_failed.
                        expected_observer_shutdown = (
                            observer_task.cancelled()
                            or self.budget.stop_requested()
                            or self.status in {'succeeded', 'stopped', 'budget_exhausted', 'completed'})
                        if observer_task.cancelled():
                            observer_outcome = {'status': 'stopped', 'reason': 'Supervisor shutdown', 'turns': observer.turns}
                        elif observer_task.exception():
                            observer_outcome = {'status': 'infra_failed', 'error': type(observer_task.exception()).__name__}
                            expected_observer_shutdown = False
                        else:
                            observer_outcome = observer_task.result()
                        self._consume_observer_outcome(observer_outcome, expected_shutdown=expected_observer_shutdown)
                if log_guard:
                    log_guard.cancel()
                    await asyncio.gather(log_guard, return_exceptions=True)
                if deliberate and tasks:
                    pending_agents = [task for task in tasks if not task.done()]
                    if pending_agents:
                        # Agents already observe budget.stop_requested() at their
                        # next boundary; let a current model call settle so the
                        # report keeps known usage, then cancel the remainder.
                        await asyncio.wait(pending_agents, timeout=shutdown_grace)
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if operation:
                    # _copy_safely waits for its thread before propagating cancellation.
                    if operation.get_name() not in {"periodic-evidence", "server-health"}:
                        operation.cancel()
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        self._record_error("maintenance_failed", exc)
                        self.status = "infra_failed"
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    try:
                        outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), self.config.get("supervision", {}).get("shutdown_timeout_s", 30))
                    except asyncio.TimeoutError as exc:
                        self.status = "infra_failed"
                        self._record_error("agent_shutdown_timeout", exc)
                if guard:
                    if guard.done() and not guard.cancelled() and guard.exception():
                        guard_error = guard.exception()
                        self.status = "resource_exhausted" if isinstance(guard_error, StorageLimitError) else "infra_failed"
                        self._record_error("resource_guard_failed", guard_error)
                    guard.cancel()
                    await asyncio.gather(guard, return_exceptions=True)
                if self.gateway:
                    try:
                        await self.gateway.close(fence=owned)
                    except Exception as exc:
                        self.status = "infra_failed"
                        self._record_error("cleanup_failed", exc)
                if owned and runtime_started and collector and not self.frozen:
                    try:
                        await self._collect(collector, server)
                    except Exception as exc:
                        self.status = "infra_failed"
                        self._record_error("evidence_collection_failed", exc)
                if owned and collector:
                    try:
                        # Drain bounded chunks even when the world is frozen or
                        # its final save failed. Partial EOF remains raw evidence
                        # with an incomplete marker, never a normalized fact.
                        await self._collect_logs(collector,final=True)
                    except Exception as exc:
                        self.status='infra_failed'
                        self._record_error('server_log_collection_failed',exc)
                for model in models:
                    close = getattr(model, "aclose", None)
                    if close:
                        try:
                            await asyncio.wait_for(close(), 5)
                        except Exception as exc:
                            self._record_error("model_cleanup_failed", exc)
                if workspaces:
                    try:
                        await workspaces.close()
                    except Exception as exc:
                        self.status = 'infra_failed'
                        self._record_error('workspace_cleanup_failed', exc)
                if self.recorder:
                    try:
                        result = await asyncio.to_thread(self._evaluation, True) if hasattr(self, "evaluator") else result
                        if self.status not in {"infra_failed", "model_failed", "reliability_failed", "resource_exhausted", "stopped", "budget_exhausted"}:
                            statuses = {x.get("status") for x in outcomes if isinstance(x, dict)}
                            if any(isinstance(x, BaseException) and not isinstance(x, asyncio.CancelledError) for x in outcomes) or "infra_failed" in statuses:
                                self.status = "infra_failed"
                            elif "model_failed" in statuses:
                                self.status = "model_failed"
                            elif result["status"] in {"succeeded", "diagnostic_pass"}:
                                self.status = result["status"]
                            elif "budget_exhausted" in statuses or time.monotonic() >= self.budget.deadline:
                                self.status = "budget_exhausted"
                            elif statuses == {"stalled"}:
                                self.status = "stalled"
                            elif statuses == {"turns_exhausted"}:
                                self.status = "not_succeeded"
                            else:
                                self.status = "not_succeeded"
                        self.recorder.set_state("active_wall_seconds", already_elapsed + time.monotonic() - start)
                        self.recorder.set_state("status", self.status)
                        self.recorder.append("agent_outcomes", {"agents": [
                            x if isinstance(x, dict) else {'status': type(x).__name__} if isinstance(x, BaseException) else x
                            for x in outcomes]})
                        self.recorder.append("run_finished", {"status": self.status, "evaluation": result, "budget": self.budget.state if self.budget else {}}, world_epoch=self.gateway.world_epoch if self.gateway else 0)
                        await asyncio.to_thread(self.recorder.export_jsonl)
                        paths = await asyncio.to_thread(write_report, self.run_dir, evaluation_result=result, task_config=self.config["experiment"]["task"])
                    except Exception as exc:
                        self.status = "infra_failed"
                        self._record_error("finalization_failed", exc)
                        try:
                            self.recorder.set_state("status", self.status)
                        except Exception:
                            pass
            finally:
                try:
                    if memory:
                        memory.close()
                finally:
                    try:
                        if self.recorder:
                            self.recorder.close()
                    finally:
                        ownership.close()
                        lock.close()
        return {"run_dir": str(self.run_dir), "status": self.status, "evaluation": result, "reports": paths}

    async def _reconcile_world(self, server):
        world = self.config["server"].get("world_dir")
        if not world:
            return
        data_dir = Path(world).parent
        if has_interrupted_restore(data_dir):
            if not server.container:
                raise RuntimeError("interrupted restore requires a managed, confirmed stopped server")
            running = await server._docker("inspect", "--format", "{{.State.Running}}", server.container)
            if running.strip() != "false":
                raise RuntimeError("interrupted restore: stop server before recovery")
            recovery = await self._copy_safely(recover_pending_restore, data_dir)
            self.recorder.append("interrupted_restore_recovered", recovery)
        receipt = await self._copy_safely(read_pending_restore_receipt, data_dir)
        if receipt:
            await self._commit_restore_receipt(receipt)

    async def _commit_restore_receipt(self, receipt):
        metadata = receipt["metadata"]
        if metadata.get("run_id") != self.recorder.run_id:
            raise RuntimeReliabilityError("pending restore belongs to another run; retain receipt for operator inspection")
        if "world_epoch" not in metadata or "restore_point_event_seq" not in metadata:
            raise RuntimeReliabilityError("restore receipt lacks scoring lineage; cannot safely continue")
        payload = {"rollback": True, "globals_lost": True, "requires_reobservation": True,
                   "restore_id": receipt["restore_id"], "checkpoint_id": receipt["checkpoint_id"],
                   "source_world_epoch": metadata["world_epoch"], "restore_point_event_seq": metadata["restore_point_event_seq"]}
        self.gateway.world_epoch = await self._copy_safely(self.recorder.apply_restore_transition,
            receipt["restore_id"], payload, self.config["agents"]["ids"])
        await self._copy_safely(acknowledge_restore_receipt, receipt["data_dir"], receipt["restore_id"])

    async def _copy_safely(self, function, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise

    async def _apply_control(self, control, server, collector):
        try:
            await self._control(control["command"], server, collector, **control["arguments"])
            self.recorder.finish_control(control["control_id"], "applied", {"status": self.status})
        except Exception as exc:
            self._record_error("control_failed", exc)
            self.recorder.finish_control(control["control_id"], "failed", {"error": str(exc)[:4000]})
            if not self.gateway.admission.is_set():
                self.status = "world_frozen" if self.frozen else "maintenance_paused"
                self.recorder.set_state("status", self.status)

    async def _control(self, command, server, collector, **arguments):
        if command in {"checkpoint", "restore"}:
            if not server.container or not self.config["server"].get("world_dir"):
                raise RuntimeError("world checkpoints require a managed container and server.world_dir")
            checkpoint_root = self.run_dir / "checkpoints"
            checkpoint_id = arguments.get("checkpoint_id")
            if command == "restore" and (not isinstance(checkpoint_id, str) or not checkpoint_id or Path(checkpoint_id).name != checkpoint_id or checkpoint_id in {".", ".."}):
                raise ValueError("restore requires a checkpoint ID inside this run")
            self.budget.admission.clear()
            self.gateway.admission.clear()
            await self.gateway.fence()
            self.sessions_fenced = True
            if not self.frozen:
                await self._collect(collector, server)
                await server.stop()
            self.frozen = True
            self.recorder.set_state("world_frozen", True)
            self.status = "world_frozen"
            await self._collect(collector, server, flush=False)
            data_dir = Path(self.config["server"]["world_dir"]).parent
            await self._reconcile_world(server)
            minimum = self.config.get("storage", {}).get("min_free_disk_bytes", 268435456)
            if command == "checkpoint":
                metadata = {"world_epoch": self.gateway.world_epoch, "run_id": self.recorder.run_id,
                    "restore_point_event_seq": self.recorder.latest_event_seq(), "created_at": datetime.now(timezone.utc).isoformat()}
                checkpoint = await self._copy_safely(create_checkpoint, data_dir, checkpoint_root, metadata, min_free_disk_bytes=minimum)
                self.recorder.set_state("latest_world_checkpoint", checkpoint)
                self.recorder.append("world_checkpoint_created", checkpoint, world_epoch=self.gateway.world_epoch)
            else:
                checkpoint = await self._copy_safely(restore_checkpoint, checkpoint_root / checkpoint_id, data_dir,
                    self.run_dir / "world_backups" / uuid.uuid4().hex, min_free_disk_bytes=minimum, expected_run_id=self.recorder.run_id)
                self.recorder.append("world_checkpoint_restored", checkpoint, world_epoch=self.gateway.world_epoch)
            if command == "restore":
                await self._commit_restore_receipt(checkpoint)
            await server.start()
            self.frozen = False
            self.recorder.set_state("world_frozen", False)
            if command == "restore":
                await self.gateway.start(paused=True)
            else:
                await self.gateway.reset_world(already_fenced=True, paused=True)
            self.sessions_fenced = False
            await self._collect(collector, server)
            await self.gateway.resume_sessions()
            self.budget.admission.set()
            self.gateway.admission.set()
            self.status = "running"
        elif command in {"pause", "freeze"}:
            if command == "freeze" and not server.container:
                raise RuntimeError("world freeze requires server.managed_container")
            self.budget.admission.clear()
            self.gateway.admission.clear()
            await self.gateway.pause_sessions()
            if command == "freeze":
                await self.gateway.fence()
                self.sessions_fenced = True
                await server.stop()
                self.frozen = True
                self.recorder.set_state("world_frozen", True)
            self.status = "world_frozen" if self.frozen else "reasoning_paused_world_running"
        elif command == "resume":
            if self.frozen or self.sessions_fenced:
                await self._reconcile_world(server)
                await server.start()
                await self.gateway.reset_world(already_fenced=True, paused=True)
                self.sessions_fenced = False
                self.frozen = False
                self.recorder.set_state("world_frozen", False)
                await self._collect(collector, server)
            await self.gateway.resume_sessions()
            self.budget.admission.set()
            self.gateway.admission.set()
            self.status = "running"
        elif command == "stop":
            self.budget.stopped = True
            self.status = "stopped"
        self.recorder.set_state("status", self.status)
        self.recorder.append("control_applied", {"command": command, "status": self.status}, world_epoch=self.gateway.world_epoch)
