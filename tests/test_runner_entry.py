"""Unit tests for the standalone runner entrypoint (``minecraft_harness.runner``).

These tests exercise the runner's own contract surface -- remote proxy payloads,
RPC-error mapping, spec scrubbing/validation, ready/finished reporting and
signal-driven cancellation -- without spawning a real control plane.  They only
need ``runner_rpc`` and the agent modules, never the supervisor.

Run in the disposable container::

    /repo/.venv/bin/python -m unittest tests.test_runner_entry -v
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import io
import json
import os
import signal
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


# The repository is a src-layout package; make the documented disposable-container
# command importable without an install.
_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from minecraft_harness import runner
from minecraft_harness.runner_rpc import RpcError


class FakeClient:
    """Records RPC calls and returns canned results (sync and async)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, float | None]] = []
        self.sync_results: dict[str, object] = {}
        self.async_results: dict[str, object] = {}
        self.sync_default: object = None
        self.async_default: object = None

    def call_sync(self, method, params=None, *, timeout_s=None):
        self.calls.append(("sync", method, dict(params or {}), timeout_s))
        value = self.sync_results.get(method, self.sync_default)
        if isinstance(value, BaseException):
            raise value
        return value

    async def call(self, method, params=None, *, timeout_s=None):
        self.calls.append(("async", method, dict(params or {}), timeout_s))
        value = self.async_results.get(method, self.async_default)
        if isinstance(value, BaseException):
            raise value
        return value

    def methods(self):
        return [call[1] for call in self.calls]

    def params_for(self, method):
        return [call[2] for call in self.calls if call[1] == method]


def _rpc_error(error_type="RuntimeError", message="remote failure", *, retryable=False):
    return RpcError(error_type, message, retryable=retryable)


def _environment(tmpdir, *, agent_id="player1", role="player"):
    root = Path(tmpdir)
    return runner.RunnerEnvironment(
        socket_path=root / "control.sock",
        token="token-value",
        agent_id=agent_id,
        role=role,
        spec_path=root / "spec.json",
        run_id="run-abc",
    )


def _spec_document(tmpdir, *, agent_id="player1", role="player"):
    return {
        "schema_version": 1,
        "run_id": "run-abc",
        "agent_id": agent_id,
        "role": role,
        "state_root": str(Path(tmpdir) / "state" / agent_id),
        "shell_enabled": False,
        "heartbeat_s": 10,
        "agent_config": {
            "agent_id": agent_id,
            "goal": "Observe the shared world.",
            "peers": ["player2"],
            "web": {"enabled": False, "api_key": "inline-secret",
                    "search_url": "https://search.test/api"},
            "shell_env": {"MC_AGENT_TOKEN": "bot-token"},
            "shell_container": "mc-workspace-deadbeef",
            "max_turns": 3,
        },
        "model": {"kind": "fake", "script": [], "api_key": "inline-model-secret"},
    }


def _write_spec(tmpdir, document):
    path = Path(tmpdir) / "spec.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    return path


def _spec(tmpdir, *, agent_id="player1", role="player"):
    document = _spec_document(tmpdir, agent_id=agent_id, role=role)
    _write_spec(tmpdir, document)
    environment = _environment(tmpdir, agent_id=agent_id, role=role)
    return environment, runner.load_spec(environment)


class ErrorMappingTests(unittest.TestCase):
    def test_known_error_types_map_to_local_classes(self):
        expected = {
            "BudgetExhausted": "BudgetExhausted",
            "ModelAdmissionError": "ModelAdmissionError",
            "StorageLimitError": "StorageLimitError",
            "RuntimeUnavailable": "RuntimeUnavailable",
            "RuntimeReliabilityError": "RuntimeReliabilityError",
            "MemoryConflict": "MemoryConflict",
            "MemoryPermissionError": "MemoryPermissionError",
            "PermissionError": "PermissionError",
            "FileNotFoundError": "FileNotFoundError",
            "ValueError": "ValueError",
            "TimeoutError": "TimeoutError",
        }
        for error_type, local_type in expected.items():
            with self.subTest(error_type=error_type):
                mapped = runner._mapped_exception(_rpc_error(error_type, "boom"))
                self.assertEqual(type(mapped).__name__, local_type)
                self.assertEqual(str(mapped), "boom")
                if error_type in {"BudgetExhausted", "StorageLimitError"}:
                    from minecraft_harness.budget import BudgetExhausted
                    from minecraft_harness.recorder import StorageLimitError

                    self.assertIsInstance(
                        mapped, {"BudgetExhausted": BudgetExhausted,
                                 "StorageLimitError": StorageLimitError}[error_type])

    def test_retryable_unknown_maps_to_connection_error(self):
        mapped = runner._mapped_exception(
            _rpc_error("ProtocolError", "peer violated protocol", retryable=True))
        self.assertIsInstance(mapped, ConnectionError)
        self.assertTrue(getattr(mapped, "retryable", False))

    def test_gateway_retryable_unknown_prefers_runtime_unavailable(self):
        from minecraft_harness.execution.gateway import RuntimeUnavailable

        mapped = runner._mapped_exception(
            _rpc_error("ConnectionError", "control plane closed", retryable=True),
            gateway=True)
        self.assertIsInstance(mapped, RuntimeUnavailable)
        self.assertTrue(mapped.retryable)

    def test_gateway_non_retryable_unknown_prefers_reliability_error(self):
        from minecraft_harness.execution.gateway import RuntimeReliabilityError

        mapped = runner._mapped_exception(
            _rpc_error("RunnerControlError", "bad gateway state"), gateway=True)
        self.assertIsInstance(mapped, RuntimeReliabilityError)
        self.assertFalse(mapped.retryable)


class RecorderProxyTests(unittest.TestCase):
    def test_append_payload_shape_and_sync(self):
        client = FakeClient()
        client.sync_results["recorder.append"] = {"event_seq": 7}
        recorder = runner.RemoteRecorder(client, agent_id="player1")
        event = recorder.append("tool_result", {"ok": True}, actor_id="player1",
                                world_epoch=2, runtime_epoch=3)
        self.assertEqual(event, {"event_seq": 7})
        method = client.calls[0]
        self.assertEqual(method[0], "sync")
        self.assertEqual(method[1], "recorder.append")
        self.assertEqual(method[2]["kind"], "tool_result")
        self.assertEqual(method[2]["payload"], {"ok": True})
        self.assertEqual(method[2]["kwargs"],
                         {"actor_id": "player1", "world_epoch": 2, "runtime_epoch": 3})

    def test_put_artifact_uses_text_when_utf8(self):
        client = FakeClient()
        client.sync_results["recorder.put_artifact"] = {"hash": "a" * 64}
        recorder = runner.RemoteRecorder(client, agent_id="observer")
        recorder.put_artifact("héllo".encode("utf-8"))
        self.assertEqual(client.params_for("recorder.put_artifact"), [{"text": "héllo"}])

    def test_put_artifact_uses_base64_for_binary(self):
        client = FakeClient()
        client.sync_results["recorder.put_artifact"] = {"hash": "a" * 64}
        recorder = runner.RemoteRecorder(client, agent_id="observer")
        recorder.put_artifact(b"\xff\xfe\x00")
        params = client.params_for("recorder.put_artifact")[0]
        self.assertNotIn("text", params)
        self.assertIn("base64", params)

    def test_read_events_and_latest_seq(self):
        client = FakeClient()
        client.sync_results["recorder.read_events"] = [{"event_seq": 1}]
        client.sync_results["recorder.latest_event_seq"] = 5
        recorder = runner.RemoteRecorder(client, agent_id="observer")
        self.assertEqual(recorder.read_events(after=3, limit=10), [{"event_seq": 1}])
        self.assertEqual(recorder.latest_event_seq(), 5)
        self.assertEqual(client.params_for("recorder.read_events"), [{"after": 3, "limit": 10}])

    def test_proxy_raises_mapped_error(self):
        client = FakeClient()
        client.sync_results["recorder.append"] = _rpc_error(
            "StorageLimitError", "ledger quota exhausted")
        recorder = runner.RemoteRecorder(client, agent_id="player1")
        from minecraft_harness.recorder import StorageLimitError

        with self.assertRaises(StorageLimitError):
            recorder.append("agent_error", {})


class BudgetProxyTests(unittest.IsolatedAsyncioTestCase):
    def test_agent_core_introspection_surface(self):
        budget = runner.RemoteBudget(FakeClient(), agent_id="player1", role="player")
        parameters = inspect.signature(budget.reserve).parameters
        self.assertIn("purpose", parameters)
        self.assertIsInstance(parameters["purpose"].default, str)
        self.assertTrue(inspect.iscoroutinefunction(budget.stop_requested))
        self.assertTrue(inspect.iscoroutinefunction(budget.admission.wait))

    async def test_reserve_and_settle_payloads(self):
        client = FakeClient()
        client.async_results["budget.reserve"] = "reservation-1"
        budget = runner.RemoteBudget(client, agent_id="player1", role="player")
        reservation = await budget.reserve("player1", purpose="agent_decision", role="player")
        self.assertEqual(reservation, "reservation-1")
        self.assertEqual(client.params_for("budget.reserve"),
                         [{"purpose": "agent_decision", "role": "player"}])
        client.async_results["budget.settle"] = None
        await budget.settle("reservation-1", {"total_tokens": 5})
        self.assertEqual(client.params_for("budget.settle"),
                         [{"reservation_id": "reservation-1",
                           "usage": {"total_tokens": 5}}])

    async def test_stop_requested_is_awaitable_bool(self):
        client = FakeClient()
        client.async_results["budget.stop_requested"] = 1
        budget = runner.RemoteBudget(client, agent_id="player1", role="player")
        self.assertIs(await budget.stop_requested(), True)

    async def test_validate_request_and_admission_wait(self):
        client = FakeClient()
        client.async_results["budget.validate_request"] = None
        client.async_results["budget.wait_admission"] = {"admitted": True, "stop": False}
        budget = runner.RemoteBudget(client, agent_id="observer", role="observer")
        await budget.validate_request(1234)
        self.assertEqual(client.params_for("budget.validate_request"),
                         [{"request_bytes": 1234}])
        await budget.admission.wait()
        self.assertIn("budget.wait_admission", client.methods())

    async def test_budget_exhausted_is_preserved(self):
        client = FakeClient()
        client.async_results["budget.reserve"] = _rpc_error(
            "BudgetExhausted", "team budget exhausted")
        budget = runner.RemoteBudget(client, agent_id="player1", role="player")
        from minecraft_harness.budget import BudgetExhausted

        with self.assertRaises(BudgetExhausted):
            await budget.reserve("player1", purpose="agent_decision", role="player")

    def test_admit_web_forwards_bound_identity_and_honors_denial(self):
        # WebTools._admit is synchronous, so the proxy must be a sync call too.
        client = FakeClient()
        client.sync_results["budget.admit_web"] = {
            "admitted": True, "agent_id": "player1", "tool": "web_search"}
        budget = runner.RemoteBudget(client, agent_id="player1", role="player")
        self.assertFalse(inspect.iscoroutinefunction(budget.admit_web))
        self.assertIs(budget.admit_web("player1", "web_search"), True)
        self.assertEqual(client.params_for("budget.admit_web"),
                         [{"agent_id": "player1", "tool": "web_search"}])
        # The bound runner identity always wins over the caller argument.
        client.sync_results["budget.admit_web"] = {"admitted": False}
        from minecraft_harness.agents.web import WebTools

        web = WebTools({"enabled": True}, budget=budget, agent_id="player1")
        self.assertIs(web._admit("web_fetch"), False)
        self.assertEqual(client.params_for("budget.admit_web")[-1],
                         {"agent_id": "player1", "tool": "web_fetch"})
        self.assertIs(budget.admit_web("intruder", "web_search"), False)
        self.assertEqual(client.params_for("budget.admit_web")[-1],
                         {"agent_id": "player1", "tool": "web_search"})


class MemoryProxyTests(unittest.TestCase):
    def test_actor_id_is_forced_to_bound_agent(self):
        client = FakeClient()
        client.sync_results["memory.read"] = {"path": "notes/plan.md", "content": "x"}
        memory = runner.RemoteMemory(client, agent_id="player1")
        memory.read("player1", scope="private", path="notes/plan.md")
        params = client.params_for("memory.read")[0]
        self.assertEqual(params["actor_id"], "player1")
        self.assertEqual(params["scope"], "private")
        self.assertEqual(params["path"], "notes/plan.md")

    def test_write_and_list_forward_tool_arguments(self):
        client = FakeClient()
        client.sync_results["memory.write"] = {"revision": 1}
        client.sync_results["memory.list"] = [{"path": "a"}]
        memory = runner.RemoteMemory(client, agent_id="player1")
        memory.write("player1", scope="private", path="a", content="c",
                     expected_revision=0, world_epoch=4)
        memory.list("player1", scope="team", offset=2, limit=10)
        self.assertEqual(client.params_for("memory.write")[0]["world_epoch"], 4)
        self.assertEqual(client.params_for("memory.list")[0],
                         {"actor_id": "player1", "scope": "team", "offset": 2, "limit": 10})

    def test_foreign_agent_is_rejected_locally(self):
        memory = runner.RemoteMemory(FakeClient(), agent_id="player1")
        with self.assertRaises(ValueError):
            memory.read("player2", scope="private", path="a")


class WorkspaceProxyTests(unittest.IsolatedAsyncioTestCase):
    def test_placeholder_attributes(self):
        workspace = runner.RemoteWorkspace(FakeClient(), shell_enabled=False)
        self.assertFalse(workspace.shell_enabled)
        self.assertIsNone(workspace.shell_container)
        self.assertEqual(workspace.shell_env, {})
        enabled = runner.RemoteWorkspace(FakeClient(), shell_enabled=True)
        self.assertTrue(enabled.shell_enabled)
        self.assertEqual(enabled.shell_container, "rpc-broker")

    async def test_file_operations_forward_exact_arguments(self):
        client = FakeClient()
        client.sync_results["workspace.read"] = {"content": "code"}
        workspace = runner.RemoteWorkspace(client, shell_enabled=True)
        self.assertEqual(workspace.read("main.js", offset=2, limit=10)["content"], "code")
        workspace.write("main.js", "const x = 1;")
        workspace.remove("old.js")
        workspace.list()
        self.assertEqual(client.params_for("workspace.read")[0],
                         {"path": "main.js", "offset": 2, "limit": 10})
        self.assertEqual(client.params_for("workspace.write")[0],
                         {"path": "main.js", "content": "const x = 1;"})
        self.assertEqual(client.params_for("workspace.remove")[0], {"path": "old.js"})
        self.assertEqual(client.params_for("workspace.list")[0], {"path": "."})

    async def test_shell_is_async_and_requires_enablement(self):
        client = FakeClient()
        client.async_results["workspace.shell"] = {"exit_code": 0, "output": "ok"}
        workspace = runner.RemoteWorkspace(client, shell_enabled=True)
        self.assertEqual((await workspace.shell("ls", timeout_s=5))["exit_code"], 0)
        self.assertEqual(client.params_for("workspace.shell")[0],
                         {"command": "ls", "timeout_s": 5})
        disabled = runner.RemoteWorkspace(FakeClient(), shell_enabled=False)
        with self.assertRaises(PermissionError):
            await disabled.shell("ls")


class ExecutorProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_payloads(self):
        client = FakeClient()
        client.async_results["gateway.execute"] = {"execution_id": "e1", "status": "succeeded"}
        client.async_results["gateway.status"] = {"execution_id": "e1", "status": "running"}
        client.async_results["gateway.read_events"] = {"events": [], "next_cursor": 0}
        executor = runner.RemoteExecutor(client, agent_id="player1")
        await executor.execute("player1", "await 1", timeout_ms=9000, request_id="r1")
        await executor.status("player1", "e1")
        await executor.read_events("player1", after_cursor=4, limit=50)
        self.assertEqual(client.params_for("gateway.execute")[0],
                         {"agent_id": "player1", "source": "await 1",
                          "timeout_ms": 9000, "request_id": "r1"})
        self.assertEqual(client.params_for("gateway.status")[0],
                         {"agent_id": "player1", "execution_id": "e1"})
        self.assertEqual(client.params_for("gateway.read_events")[0],
                         {"agent_id": "player1", "after_cursor": 4, "limit": 50})

    async def test_all_tools_carry_the_bound_identity(self):
        client = FakeClient()
        client.async_results["gateway.wait"] = {"status": "succeeded"}
        client.async_results["gateway.jobs"] = {"jobs": []}
        client.async_results["gateway.cancel_job"] = {"job_id": "j"}
        executor = runner.RemoteExecutor(client, agent_id="player1")
        await executor.wait("player1", "e1", wait_ms=500)
        await executor.jobs("player1")
        await executor.cancel_job("player1", "j1")
        self.assertEqual(client.params_for("gateway.wait")[0],
                         {"agent_id": "player1", "execution_id": "e1", "wait_ms": 500})
        self.assertEqual(client.params_for("gateway.jobs")[0], {"agent_id": "player1"})
        self.assertEqual(client.params_for("gateway.cancel_job")[0],
                         {"agent_id": "player1", "job_id": "j1"})

    async def test_gateway_error_is_runtime_unavailable(self):
        client = FakeClient()
        client.async_results["gateway.status"] = _rpc_error(
            "RuntimeUnavailable", "bot runtime is down", retryable=True)
        executor = runner.RemoteExecutor(client, agent_id="player1")
        from minecraft_harness.execution.gateway import RuntimeUnavailable

        with self.assertRaises(RuntimeUnavailable):
            await executor.status("player1", "e1")

    async def test_foreign_agent_execution_is_rejected(self):
        executor = runner.RemoteExecutor(FakeClient(), agent_id="player1")
        with self.assertRaises(ValueError):
            await executor.status("player2", "e1")


class ArtifactProxyTests(unittest.TestCase):
    def test_grant_then_owned_uses_local_cache(self):
        client = FakeClient()
        client.sync_results["artifact.owned"] = True
        artifacts = runner.RemoteArtifactAPI(client, agent_id="player1")
        digest = "b" * 64
        artifacts.grant("player1", digest)
        self.assertTrue(artifacts.owned("player1", digest))
        self.assertTrue(artifacts.owned("player1", digest))
        # A granted digest must never be re-checked over RPC.
        self.assertEqual(client.methods(), ["artifact.grant"])

    def test_owned_caches_a_remote_grant(self):
        client = FakeClient()
        client.sync_results["artifact.owned"] = True
        artifacts = runner.RemoteArtifactAPI(client, agent_id="player1")
        digest = "e" * 64
        self.assertTrue(artifacts.owned("player1", digest))
        self.assertTrue(artifacts.owned("player1", digest))
        self.assertEqual(client.methods(), ["artifact.owned"])

    def test_owned_remote_result_is_coerced_to_bool(self):
        client = FakeClient()
        client.sync_results["artifact.owned"] = {"owned": False}
        artifacts = runner.RemoteArtifactAPI(client, agent_id="player1")
        self.assertIs(artifacts.owned("player1", "c" * 64), False)

    def test_read_forwards_digest_offset_and_limit(self):
        client = FakeClient()
        client.sync_results["artifact.read"] = {"hash": "d" * 64, "content": "x"}
        artifacts = runner.RemoteArtifactAPI(client, agent_id="observer")
        artifacts.read("observer", "d" * 64, 10, 20)
        self.assertEqual(client.params_for("artifact.read"),
                         [{"hash": "d" * 64, "offset": 10, "limit": 20}])

    def test_invalid_digest_is_rejected(self):
        artifacts = runner.RemoteArtifactAPI(FakeClient(), agent_id="player1")
        with self.assertRaises(ValueError):
            artifacts.owned("player1", "not-a-digest")


class InfraCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_issue_is_wrapped_for_supervisor(self):
        client = FakeClient()
        client.async_results["supervisor.observer_infra_issue"] = {"accepted": True}
        callback = runner.RemoteInfraCallback(client, agent_id="observer")
        issue = {"issue_id": "x", "kind": "broken_tool", "reason": "r",
                 "evidence_refs": [1]}
        self.assertEqual(await callback(issue), {"accepted": True})
        self.assertEqual(client.params_for("supervisor.observer_infra_issue"),
                         [{"issue": issue}])


class SpecTests(unittest.TestCase):
    def test_valid_spec_is_scrubbed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            environment, spec = _spec(tmpdir)
        self.assertEqual(spec.agent_id, "player1")
        self.assertEqual(spec.role, "player")
        self.assertEqual(spec.heartbeat_s, 10.0)
        self.assertFalse(spec.shell_enabled)
        self.assertNotIn("api_key", spec.model)
        self.assertEqual(spec.model["kind"], "fake")
        config = spec.agent_config
        self.assertEqual(config["agent_id"], "player1")
        self.assertNotIn("shell_env", config)
        self.assertNotIn("shell_container", config)
        self.assertNotIn("api_key", config["web"])
        self.assertEqual(config["web"]["search_url"], "https://search.test/api")

    def test_environment_requires_every_variable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            environment = _environment(tmpdir)
            missing = {
                "HARNESS_RUNNER_SOCKET": str(environment.socket_path),
                "HARNESS_RUNNER_TOKEN": "t",
                "HARNESS_RUNNER_AGENT": "player1",
                "HARNESS_RUNNER_ROLE": "player",
                "HARNESS_RUNNER_SPEC": str(environment.spec_path),
            }
            parsed = runner.RunnerEnvironment.from_environment(missing)
            self.assertEqual(parsed.agent_id, "player1")
            for name in list(missing):
                broken = {key: value for key, value in missing.items() if key != name}
                with self.subTest(missing=name):
                    with self.assertRaises(runner.RunnerStartupError):
                        runner.RunnerEnvironment.from_environment(broken)

    def test_identity_mismatch_is_a_startup_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            document = _spec_document(tmpdir, agent_id="player2")
            path = _write_spec(tmpdir, document)
            environment = runner.RunnerEnvironment(
                socket_path=Path(tmpdir) / "control.sock", token="t",
                agent_id="player1", role="player", spec_path=path)
            with self.assertRaises(runner.RunnerStartupError):
                runner.load_spec(environment)
            self.assertTrue(path.exists())

    def test_invalid_spec_documents_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = _spec_document(tmpdir)
            mutations = [
                {"schema_version": 2},
                {"role": "supervisor"},
                {"state_root": "relative/path"},
                {"heartbeat_s": 0},
                {"shell_enabled": "yes"},
            ]
            for mutation in mutations:
                with self.subTest(mutation=mutation):
                    document = {**base, **mutation}
                    path = _write_spec(tmpdir, document)
                    environment = runner.RunnerEnvironment(
                        socket_path=Path(tmpdir) / "control.sock", token="t",
                        agent_id="player1", role="player", spec_path=path)
                    with self.assertRaises(runner.RunnerStartupError):
                        runner.load_spec(environment)

    def test_symlinked_spec_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            real = _write_spec(tmpdir, _spec_document(tmpdir))
            link = Path(tmpdir) / "spec-link.json"
            os.symlink(real, link)
            environment = runner.RunnerEnvironment(
                socket_path=Path(tmpdir) / "control.sock", token="t",
                agent_id="player1", role="player", spec_path=link)
            with self.assertRaises(runner.RunnerStartupError):
                runner.load_spec(environment)


class PrepareRunnerTests(unittest.TestCase):
    def test_player_agent_and_ready_hashes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            environment, spec = _spec(tmpdir)
            prepared = runner.prepare_runner(environment, spec)
            self.assertEqual(prepared.ready_params["agent_id"], "player1")
            self.assertEqual(prepared.ready_params["role"], "player")
            self.assertEqual(prepared.ready_params["pid"], os.getpid())
            prompt = prepared.agent.messages[0]["content"]
            expected_prompt = hashlib.sha256(prompt.encode()).hexdigest()
            expected_tools = hashlib.sha256(
                json.dumps(prepared.agent.schemas, sort_keys=True).encode()).hexdigest()
            self.assertEqual(prepared.ready_params["prompt_sha256"], expected_prompt)
            self.assertEqual(prepared.ready_params["tools_sha256"], expected_tools)
            # The remote workspace disables shell because the spec says so.
            self.assertFalse(prepared.agent.workspace.shell_enabled)

    def test_observer_agent_uses_remote_infra_callback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            environment, spec = _spec(tmpdir, agent_id="observer", role="observer")
            prepared = runner.prepare_runner(environment, spec)
            from minecraft_harness.observer.agent import ObserverAgent

            self.assertIsInstance(prepared.agent, ObserverAgent)
            self.assertIsInstance(prepared.agent.infra_callback,
                                  runner.RemoteInfraCallback)
            self.assertEqual(prepared.ready_params["role"], "observer")


class _DummyAgent:
    def __init__(self, result=None, turns=3):
        self.turns = turns
        self._result = result or {"agent_id": "player1",
                                  "status": "turns_exhausted", "turns": turns}
        self.cancelled = False

    async def run(self):
        return dict(self._result)


class _BlockingAgent:
    turns = 2

    def __init__(self):
        self.cancelled = False

    async def run(self):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _prepared(tmpdir, fake, agent, *, heartbeat_s=10.0):
    environment, spec = _spec(tmpdir)
    return runner.PreparedRunner(
        environment=environment, spec=spec, client=fake, agent=agent,
        heartbeat_s=heartbeat_s,
        ready_params={"agent_id": environment.agent_id, "role": environment.role,
                      "pid": os.getpid(), "prompt_sha256": "a" * 64,
                      "tools_sha256": "b" * 64})


class RunLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Keep the test process' real signal handlers out of these unit tests;
        # the cancellation test installs its own controllable stop event.
        patcher = mock.patch.object(runner, "_install_stop_signal_handlers",
                                    lambda stop_event: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_completion_sends_ready_then_finished(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = FakeClient()
            agent = _DummyAgent()
            prepared = _prepared(tmpdir, fake, agent)
            stdout = io.StringIO()
            code = await runner._run_prepared(prepared, threading.Event(), stdout)
        self.assertEqual(code, 0)
        self.assertEqual(fake.methods(), ["runner.ready", "runner.finished"])
        self.assertEqual(fake.params_for("runner.ready")[0], prepared.ready_params)
        self.assertEqual(fake.params_for("runner.finished")[0],
                         {"result": {"agent_id": "player1",
                                     "status": "turns_exhausted", "turns": 3}})
        # No stdout is written when runner.finished is delivered.
        self.assertEqual(stdout.getvalue(), "")

    async def test_ready_failure_raises_startup_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = FakeClient()
            fake.async_results["runner.ready"] = _rpc_error(
                "ConnectionError", "control plane is down", retryable=True)
            prepared = _prepared(tmpdir, fake, _DummyAgent())
            with self.assertRaises(runner.RunnerStartupError):
                await runner._run_prepared(prepared, threading.Event(), io.StringIO())
            self.assertNotIn("runner.finished", fake.methods())

    async def test_signal_cancels_agent_and_reports_stopped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = FakeClient()
            agent = _BlockingAgent()
            prepared = _prepared(tmpdir, fake, agent, heartbeat_s=0.01)
            stop_events = []

            def fake_install(stop_event):
                stop_events.append(stop_event)

            stdout = io.StringIO()
            with mock.patch.object(runner, "_install_stop_signal_handlers", fake_install):
                task = asyncio.create_task(
                    runner._run_prepared(prepared, threading.Event(), stdout))
                await asyncio.sleep(0.05)
                self.assertTrue(stop_events, "the stop handler was never installed")
                stop_events[0].set()
                code = await asyncio.wait_for(task, 5)
        self.assertEqual(code, 0)
        self.assertTrue(agent.cancelled)
        self.assertEqual(fake.params_for("runner.finished")[0],
                         {"result": {"agent_id": "player1", "status": "stopped", "turns": 2}})
        self.assertEqual(stdout.getvalue(), "")

    async def test_finished_delivery_failure_writes_stdout_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = FakeClient()
            fake.async_results["runner.finished"] = _rpc_error(
                "ConnectionError", "control plane closed", retryable=True)
            prepared = _prepared(tmpdir, fake, _DummyAgent())
            stdout = io.StringIO()
            code = await runner._run_prepared(prepared, threading.Event(), stdout)
        self.assertEqual(code, 0)
        self.assertEqual(fake.methods().count("runner.finished"), 2)
        line = stdout.getvalue().strip()
        document = json.loads(line)
        self.assertEqual(document["__runner_result__"]["status"], "turns_exhausted")

    async def test_early_stop_before_ready_starts_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = FakeClient()
            early = threading.Event()
            early.set()
            prepared = _prepared(tmpdir, fake, _DummyAgent())
            code = await runner._run_prepared(prepared, early, io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(fake.methods(), [])

    async def test_heartbeat_reports_turns_status_and_rss(self):
        fake = FakeClient()
        agent = _DummyAgent(turns=4)
        state = {"status": "running"}
        task = asyncio.create_task(
            runner._heartbeat_loop(fake, agent, 0.01, state))
        await asyncio.sleep(0.06)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        heartbeats = fake.params_for("runner.heartbeat")
        self.assertTrue(heartbeats)
        self.assertEqual(heartbeats[0]["turns"], 4)
        self.assertEqual(heartbeats[0]["status"], "running")
        self.assertIsInstance(heartbeats[0]["rss_bytes"], int)
        self.assertGreaterEqual(heartbeats[0]["rss_bytes"], 0)


class _FakeHub:
    """Minimal stand-in for ServiceHub used by the process smoke tests."""

    def __init__(self) -> None:
        self.events = {"ready": asyncio.Event(), "finished": asyncio.Event()}
        self.seen: dict[str, object] = {"ready": None, "finished": None, "methods": []}
        self._sequence = 0

    async def handle(self, agent_id, role, method, params):
        self.seen["methods"].append(method)  # type: ignore[union-attr]
        params = params if isinstance(params, dict) else {}
        if method == "runner.ready":
            self.seen["ready"] = dict(params)
            self.events["ready"].set()
            return {"accepted": True}
        if method == "runner.heartbeat":
            return {"accepted": True}
        if method == "runner.finished":
            self.seen["finished"] = dict(params)
            self.events["finished"].set()
            return {"accepted": True}
        if method == "budget.validate_request":
            return None
        if method == "budget.reserve":
            return "reservation-1"
        if method == "budget.settle":
            return None
        if method == "budget.stop_requested":
            return False
        if method == "budget.wait_admission":
            return {"admitted": True, "stop": False}
        if method == "recorder.append":
            self._sequence += 1
            return {"event_seq": self._sequence, "kind": params.get("kind"),
                    "payload": params.get("payload"), "actor_id": agent_id}
        if method == "recorder.put_artifact":
            if "text" in params:
                data = str(params["text"]).encode("utf-8")
            else:
                data = base64.b64decode(str(params.get("base64", "")))
            digest = hashlib.sha256(data).hexdigest()
            return {"hash": digest, "sha256": digest, "path": "artifacts/" + digest}
        if method == "gateway.read_events":
            return {"events": [], "next_cursor": 0}
        if method == "workspace.write":
            return {"path": params.get("path"),
                    "bytes": len(str(params.get("content", "")))}
        if method == "artifact.grant":
            return None
        raise AssertionError(f"unexpected runner RPC method: {method}")


def _player_agent_config(**overrides):
    config = {
        "agent_id": "player1",
        "goal": "Write a short note, then stop.",
        "peers": [],
        "lifecycle": "strict",
        "web": {"enabled": False},
        "game_chat": True,
        "chat_delivery": "events",
        "model_request_timeout_s": 60,
        "max_turns": 2,
        "max_no_progress_turns": 5,
        "max_model_failures": 2,
        "context_chars": 48000,
    }
    config.update(overrides)
    return config


def _write_runner_spec(tmpdir, *, state_root, agent_config, model):
    spec_path = Path(tmpdir) / "player1.json"
    document = {
        "schema_version": 1,
        "run_id": "smoke-run",
        "agent_id": "player1",
        "role": "player",
        "state_root": str(state_root),
        "shell_enabled": False,
        "heartbeat_s": 10,
        "agent_config": agent_config,
        "model": model,
    }
    descriptor = os.open(spec_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    return spec_path


class RunnerProcessSmokeTests(unittest.IsolatedAsyncioTestCase):
    """Launch the real ``python -m minecraft_harness.runner`` against a fake hub.

    These are intentionally self-contained (no Supervisor, no Minecraft runtime):
    they exercise the module entrypoint, environment/spec loading, the real RPC
    wire protocol in both sync and async direction, a scripted fake model, and
    the runner.ready/heartbeat/finished lifecycle including SIGTERM shutdown.
    """

    async def _start(self, tmpdir, hub, agent_config, model):
        from minecraft_harness.runner_rpc import RpcServer

        socket_path = Path(tmpdir) / "control.sock"
        state_root = Path(tmpdir) / "state"
        token = "tok-" + hashlib.sha256(tmpdir.encode()).hexdigest()[:16]
        spec_path = _write_runner_spec(tmpdir, state_root=state_root,
                                agent_config=agent_config, model=model)
        server = RpcServer(socket_path, {token: {"agent_id": "player1",
                                                 "role": "player"}}, hub.handle)
        await server.start()
        environment = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONPATH": str(_SOURCE_ROOT),
            "HOME": tmpdir,
            "PYTHONUNBUFFERED": "1",
            "HARNESS_RUNNER_SOCKET": str(socket_path),
            "HARNESS_RUNNER_TOKEN": token,
            "HARNESS_RUNNER_AGENT": "player1",
            "HARNESS_RUNNER_ROLE": "player",
            "HARNESS_RUNNER_SPEC": str(spec_path),
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "minecraft_harness.runner",
            cwd=tmpdir, env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        return server, process, state_root

    async def _stop(self, server, process):
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        await server.close()

    async def test_runner_subprocess_completes_a_scripted_player_run(self):
        hub = _FakeHub()
        with tempfile.TemporaryDirectory() as tmpdir:
            server, process, state_root = await self._start(
                tmpdir, hub, _player_agent_config(),
                {"kind": "fake", "script": [{
                    "tool_calls": [{
                        "id": "call-1", "type": "function",
                        "function": {
                            "name": "workspace_write",
                            "arguments": json.dumps({"path": "notes/plan.txt",
                                                     "content": "observe the world"}),
                        },
                    }],
                }]})
            try:
                try:
                    await asyncio.wait_for(hub.events["ready"].wait(), 30)
                    await asyncio.wait_for(hub.events["finished"].wait(), 60)
                except TimeoutError:
                    if process.returncode is None:
                        process.kill()
                    stdout, stderr = await process.communicate()
                    self.fail(f"runner never reported; stdout={stdout!r} "
                              f"stderr={stderr!r} methods={hub.seen['methods']!r}")
                stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
                self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
                self.assertEqual(stdout, b"")
                ready = hub.seen["ready"]
                self.assertEqual(ready["agent_id"], "player1")
                self.assertEqual(ready["role"], "player")
                self.assertEqual(len(ready["prompt_sha256"]), 64)
                self.assertEqual(len(ready["tools_sha256"]), 64)
                result = hub.seen["finished"]["result"]
                self.assertEqual(result["agent_id"], "player1")
                self.assertEqual(result["status"], "turns_exhausted")
                self.assertEqual(result["turns"], 2)
                self.assertIn("workspace.write", hub.seen["methods"])
                self.assertTrue((state_root / "checkpoint.json").is_file())
            finally:
                await self._stop(server, process)

    async def test_sigterm_stops_the_agent_and_reports_stopped(self):
        hub = _FakeHub()
        with tempfile.TemporaryDirectory() as tmpdir:
            server, process, _state_root = await self._start(
                tmpdir, hub,
                _player_agent_config(max_turns=1_000_000,
                                     max_no_progress_turns=1_000_000),
                {"kind": "fake", "script": []})
            try:
                await asyncio.wait_for(hub.events["ready"].wait(), 30)
                process.send_signal(signal.SIGTERM)
                await asyncio.wait_for(hub.events["finished"].wait(), 30)
                stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
                self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
                self.assertEqual(stdout, b"")
                result = hub.seen["finished"]["result"]
                self.assertEqual(result["agent_id"], "player1")
                self.assertEqual(result["status"], "stopped")
                self.assertIsInstance(result["turns"], int)
                self.assertGreaterEqual(result["turns"], 0)
            finally:
                await self._stop(server, process)


class EntrypointTests(unittest.TestCase):
    def test_main_rejects_arguments(self):
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr), \
                mock.patch.object(sys, "stdout", io.StringIO()):
            code = runner.main(["unexpected"])
        self.assertEqual(code, 2)
        document = json.loads(stderr.getvalue().strip())
        self.assertIn("__runner_error__", document)

    def test_main_reports_missing_environment(self):
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(sys, "stderr", stderr), \
                mock.patch.object(sys, "stdout", io.StringIO()):
            code = runner.main([])
        self.assertEqual(code, 2)
        document = json.loads(stderr.getvalue().strip())
        self.assertEqual(document["__runner_error__"]["error"], "RunnerStartupError")


if __name__ == "__main__":
    unittest.main()
