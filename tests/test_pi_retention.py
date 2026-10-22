"""Retention-policy tests for the Pi runtime.

Covers the bounded Pi session policy (pre-spawn prune / rotation / archive cap,
active-session guard) and the shared artifact budget (metadata-only fallback and
one-time retention event), plus spec/config validation of the retention knobs.

Run in the disposable test env::

    PYTHONPATH=$PWD/src:$PWD/.venv/lib/python3.11/site-packages \
        .test-venv/bin/python -m pytest -q tests/test_pi_retention.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from minecraft_harness import pi_runner
from minecraft_harness.config import validate_config
from minecraft_harness.pi_model_proxy import (ArtifactBudget,
                                              PiModelProxy,
                                              budgeted_artifact_text)

REPO = Path(__file__).resolve().parents[1]
PI_CLI = (REPO / "pi_runtime" / "node_modules" / "@earendil-works" /
          "pi-coding-agent" / "dist" / "bundle" / "cli.js")


class _FakeClient:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def call_sync(self, method, params=None, *, timeout_s=None):
        if method == "recorder.append":
            self.events.append((params["kind"], params["payload"]))
            return {"event_seq": len(self.events)}
        raise AssertionError(method)


def _write_session(path: Path, size: int, *, age_seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))


def _fake_prepared(root: Path, *, session_max: int):
    session_dir = root / "pi-sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    append_event = lambda kind, payload: _fake_prepared.events.append((kind, payload))  # noqa: E731
    _fake_prepared.events = []
    prepared = SimpleNamespace(
        paths=SimpleNamespace(session_dir=session_dir),
        spec=SimpleNamespace(pi_session_max_bytes=session_max,
                             agent_id="player1"),
        append_event=append_event,
    )
    return prepared


class SessionRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.session_dir = self.root / "pi-sessions"
        self.session_dir.mkdir()
        self.state_root = self.root

    def test_prunes_oldest_sessions_and_keeps_the_newest_for_resume(self):
        for index in range(5):
            _write_session(self.session_dir / f"session-{index}.jsonl", 300,
                           age_seconds=100 - index * 10)
        result = pi_runner.apply_session_retention(
            self.session_dir, max_bytes=900, state_root=self.state_root)
        self.assertGreater(result.total_before, 900)
        self.assertLessEqual(result.total_after, 900)
        self.assertTrue(result.resume)
        self.assertFalse(result.rotated)
        self.assertIsNone(result.archived)
        self.assertEqual(sorted(item["path"] for item in result.pruned),
                         ["session-0.jsonl", "session-1.jsonl"])
        self.assertEqual(sorted(path.name for path in self.session_dir.glob("*.jsonl")),
                         ["session-2.jsonl", "session-3.jsonl", "session-4.jsonl"])

    def test_small_sessions_are_not_pruned(self):
        _write_session(self.session_dir / "one.jsonl", 100, age_seconds=10)
        result = pi_runner.apply_session_retention(
            self.session_dir, max_bytes=1000, state_root=self.state_root)
        self.assertEqual(result.total_before, 100)
        self.assertEqual(result.total_after, 100)
        self.assertTrue(result.resume)
        self.assertEqual(result.pruned, [])

    def test_empty_session_dir_starts_fresh(self):
        result = pi_runner.apply_session_retention(
            self.session_dir, max_bytes=1000, state_root=self.state_root)
        self.assertEqual(result.total_before, 0)
        self.assertEqual(result.total_after, 0)
        self.assertFalse(result.resume)
        self.assertFalse(result.rotated)

    def test_oversized_active_session_is_archived_and_not_resumed(self):
        _write_session(self.session_dir / "old.jsonl", 100, age_seconds=100)
        _write_session(self.session_dir / "active.jsonl", 5000, age_seconds=1)
        result = pi_runner.apply_session_retention(
            self.session_dir, max_bytes=3000, state_root=self.state_root)
        self.assertTrue(result.rotated)
        self.assertFalse(result.resume)
        self.assertEqual(result.pruned[0]["path"], "old.jsonl")
        self.assertEqual(result.total_after, 0)
        self.assertFalse((self.session_dir / "active.jsonl").exists())
        archived_files = list((self.root / "pi-sessions-archive").rglob("*.jsonl"))
        self.assertEqual(len(archived_files), 1)
        self.assertEqual(archived_files[0].stat().st_size, 5000)
        self.assertIsNotNone(result.archived)

    def test_archive_itself_is_bounded(self):
        archive_root = self.root / "pi-sessions-archive"
        for index in range(12):
            _write_session(archive_root / f"run-{index}" / "active.jsonl", 200,
                           age_seconds=100 - index)
        pruned = pi_runner._prune_session_archive(
            archive_root, max_bytes=10_000, max_files=5)
        self.assertEqual(len(pruned), 7)
        remaining = list(archive_root.rglob("*.jsonl"))
        self.assertEqual(len(remaining), 5)

    def test_retention_events_are_recorded(self):
        _write_session(self.session_dir / "old.jsonl", 100, age_seconds=100)
        _write_session(self.session_dir / "active.jsonl", 5000, age_seconds=1)
        result = pi_runner.apply_session_retention(
            self.session_dir, max_bytes=3000, state_root=self.state_root)
        client = _FakeClient()
        pi_runner._record_session_retention(client, "player1", result, max_bytes=3000)
        kinds = [kind for kind, _payload in client.events]
        self.assertIn("pi_session_pruned", kinds)
        self.assertIn("pi_session_rotated", kinds)
        pruned = next(payload for kind, payload in client.events
                      if kind == "pi_session_pruned")
        self.assertEqual(pruned["total_before"], 5100)
        self.assertLessEqual(pruned["total_after"], 3000)
        rotated = next(payload for kind, payload in client.events
                       if kind == "pi_session_rotated")
        self.assertEqual(rotated["reason"], "pi_session_max_bytes")
        self.assertFalse(rotated["resume"])

    def test_rotated_session_uses_recovery_prompt(self):
        rotated = SimpleNamespace(session_rotated=True, session_resume=False)
        resumed = SimpleNamespace(session_rotated=False, session_resume=True)
        fresh = SimpleNamespace(session_rotated=False, session_resume=False)
        self.assertEqual(pi_runner._initial_prompt(rotated), pi_runner.ROTATED_PROMPT)
        self.assertEqual(pi_runner._initial_prompt(resumed), pi_runner.RESUME_PROMPT)
        self.assertEqual(pi_runner._initial_prompt(fresh), pi_runner.INITIAL_PROMPT)

    def test_active_session_guard_stops_the_runner(self):
        prepared = _fake_prepared(self.root, session_max=1000)
        _write_session(prepared.paths.session_dir / "active.jsonl", 2000,
                       age_seconds=1)
        stop_event = asyncio.Event()
        state: dict = {}
        triggered = pi_runner._check_active_session_limit(prepared, state, stop_event)
        self.assertTrue(triggered)
        self.assertTrue(stop_event.is_set())
        self.assertTrue(state["session_limit"])
        self.assertEqual(state["session_bytes"], 2000)
        kinds = [kind for kind, _payload in _fake_prepared.events]
        self.assertEqual(kinds, ["pi_retention_pressure", "pi_session_size_limit"])
        limit_event = next(payload for kind, payload in _fake_prepared.events
                           if kind == "pi_session_size_limit")
        self.assertEqual(limit_event["limit_bytes"], 1000)
        self.assertEqual(limit_event["bytes"], 2000)

    def test_active_session_guard_is_quiet_under_the_limit(self):
        prepared = _fake_prepared(self.root, session_max=1000)
        _write_session(prepared.paths.session_dir / "active.jsonl", 500,
                       age_seconds=1)
        stop_event = asyncio.Event()
        self.assertFalse(pi_runner._check_active_session_limit(prepared, {}, stop_event))
        self.assertFalse(stop_event.is_set())

    def test_guard_interval_override_for_tests(self):
        old = os.environ.get(pi_runner.ENV_PI_SESSION_GUARD_S)
        try:
            os.environ[pi_runner.ENV_PI_SESSION_GUARD_S] = "0.5"
            self.assertEqual(pi_runner._session_guard_interval(), 0.5)
            os.environ[pi_runner.ENV_PI_SESSION_GUARD_S] = "not-a-number"
            self.assertEqual(pi_runner._session_guard_interval(),
                             pi_runner.SESSION_GUARD_SECONDS)
        finally:
            if old is None:
                os.environ.pop(pi_runner.ENV_PI_SESSION_GUARD_S, None)
            else:
                os.environ[pi_runner.ENV_PI_SESSION_GUARD_S] = old


class SpecAndConfigTests(unittest.TestCase):
    def _environment(self, root: Path) -> pi_runner.PiEnvironment:
        return pi_runner.PiEnvironment(
            socket_path=root / "control.sock", token="token",
            agent_id="player1", role="player",
            spec_path=root / "spec.json", run_id="run-1")

    def _document(self, root: Path, **overrides):
        document = {
            "schema_version": 1, "run_id": "run-1", "agent_id": "player1",
            "role": "player", "runner_runtime": "pi",
            "state_root": str(root / "state"),
            "pi_cli": str(PI_CLI), "pi_context_window": 64000,
            "heartbeat_s": 10,
            "agent_config": {"agent_id": "player1", "goal": "observe"},
            "pi_model": {"base_url": "https://example.test/v1", "model": "m"},
        }
        document.update(overrides)
        return document

    def test_spec_carries_retention_caps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = pi_runner._validate_spec(self._document(
                root, pi_session_max_bytes=123456, pi_artifact_max_bytes=654321),
                self._environment(root))
            self.assertEqual(spec.pi_session_max_bytes, 123456)
            self.assertEqual(spec.pi_artifact_max_bytes, 654321)
            defaults = pi_runner._validate_spec(self._document(root),
                                                self._environment(root))
            self.assertEqual(defaults.pi_session_max_bytes,
                             pi_runner.DEFAULT_PI_SESSION_MAX_BYTES)
            self.assertEqual(defaults.pi_artifact_max_bytes,
                             pi_runner.DEFAULT_PI_ARTIFACT_MAX_BYTES)

    def test_invalid_retention_caps_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for overrides in ({"pi_session_max_bytes": 0},
                              {"pi_session_max_bytes": "big"},
                              {"pi_artifact_max_bytes": -1},
                              {"pi_artifact_max_bytes": True}):
                with self.subTest(overrides=overrides):
                    with self.assertRaises(pi_runner.PiRunnerError):
                        pi_runner._validate_spec(self._document(root, **overrides),
                                                 self._environment(root))

    def test_config_validation_of_retention_knobs(self):
        defaults = validate_config({})
        self.assertEqual(defaults["storage"]["pi_session_max_bytes"],
                         1_000_000_000)
        self.assertEqual(defaults["storage"]["pi_artifact_max_bytes"],
                         4_000_000_000)
        with self.assertRaises(ValueError):
            validate_config({"storage": {"max_run_bytes": 1024,
                                         "pi_artifact_max_bytes": 2048}})
        with self.assertRaises(ValueError):
            validate_config({"storage": {"pi_session_max_bytes": 0}})


class ArtifactBudgetTests(unittest.TestCase):
    def test_full_artifacts_are_stored_while_under_the_cap(self):
        retained: list[dict] = []
        budget = ArtifactBudget(100, event=retained.append)
        text = "a" * 40
        self.assertEqual(budgeted_artifact_text(budget, "model_request", text), text)
        self.assertEqual(budget.used_bytes, 40)
        self.assertEqual(retained, [])
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["used_bytes"], 40)
        self.assertEqual(snapshot["limit_bytes"], 100)

    def test_over_cap_artifacts_become_bounded_metadata(self):
        retained: list[dict] = []
        budget = ArtifactBudget(50, event=retained.append)
        first = budgeted_artifact_text(budget, "tool_result", "y" * 100)
        second = budgeted_artifact_text(budget, "compaction", "z" * 100)
        for text, kind, size in ((first, "tool_result", 100), (second, "compaction", 100)):
            projection = json.loads(text)
            self.assertEqual(projection["retention"], "metadata_only")
            self.assertEqual(projection["kind"], kind)
            self.assertEqual(projection["bytes"], size)
            self.assertEqual(len(projection["sha256"]), 64)
            self.assertTrue(projection["full_text_omitted"])
            self.assertLess(len(text), 2500)
        # Metadata projections are charged too, so fallbacks cannot grow freely.
        self.assertGreater(budget.used_bytes, 0)
        self.assertGreater(budget.metadata_bytes, 0)
        self.assertEqual(budget.retained_count, 2)
        retention_events = [item for item in retained
                            if item.get("event") == "pi_artifact_retention"]
        self.assertEqual(len(retention_events), 1)  # retention event is one-time
        self.assertEqual(retention_events[0]["reason"], "pi_artifact_max_bytes")
        self.assertEqual(retention_events[0]["limit_bytes"], 50)

    def test_budget_never_exceeds_its_cap(self):
        budget = ArtifactBudget(10)
        self.assertTrue(budget.take(10))
        self.assertFalse(budget.take(1))
        self.assertFalse(budget.take(10_000))
        self.assertEqual(budget.used_bytes, 10)

    def test_metadata_projection_preserves_head_and_tail(self):
        text = "HEAD" + ("m" * 5000) + "TAIL"
        projection = json.loads(ArtifactBudget.metadata_text("model_response", text))
        self.assertTrue(projection["head"].startswith("HEAD"))
        self.assertTrue(projection["tail"].endswith("TAIL"))
        self.assertLessEqual(len(projection["head"]), 512)
        self.assertLessEqual(len(projection["tail"]), 512)


class _FakeRpc:
    def __init__(self):
        import threading

        self.lock = threading.Lock()
        self.events: list[dict] = []
        self.artifacts: list[str] = []
        self.settled = threading.Event()

    def call_sync(self, method, params=None, *, timeout_s=None):
        params = params or {}
        if method == "recorder.append":
            with self.lock:
                self.events.append({"kind": params.get("kind"),
                                    "payload": params.get("payload")})
            return {"event_seq": len(self.events)}
        if method == "recorder.put_artifact":
            text = str(params.get("text", ""))
            with self.lock:
                self.artifacts.append(text)
            import hashlib

            digest = hashlib.sha256(text.encode()).hexdigest()
            return {"hash": digest, "sha256": digest, "path": "artifacts/" + digest}
        if method == "budget.wait_admission":
            return {"admitted": True, "stop": False}
        if method == "budget.validate_request":
            return None
        if method == "budget.reserve":
            return "res-1"
        if method == "budget.settle":
            self.settled.set()
            return None
        raise AssertionError(method)

    def kinds(self) -> list[str]:
        with self.lock:
            return [event["kind"] for event in self.events]


class _FakeUpstream:
    def __init__(self, payload: bytes):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("content-length", "0")))
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


class ProxyArtifactBudgetTests(unittest.TestCase):
    def test_proxy_stores_metadata_only_artifacts_after_the_cap(self):
        sse = (b'data: {"id":"c","object":"chat.completion.chunk","model":"fake",'
               b'"choices":[{"index":0,"delta":{"role":"assistant","content":"hello"},'
               b'"finish_reason":"stop"}],"usage":{"prompt_tokens":5,'
               b'"completion_tokens":2,"total_tokens":7}}\n\n'
               b'data: [DONE]\n\n')
        upstream = _FakeUpstream(sse)
        self.addCleanup(upstream.stop)
        rpc = _FakeRpc()
        retained: list[dict] = []
        budget = ArtifactBudget(10, event=retained.append)
        proxy = PiModelProxy({"base_url": upstream.base_url, "model": "fake-upstream",
                              "timeout_s": 10}, rpc, agent_id="player1",
                             artifact_budget=budget)
        proxy.start()
        self.addCleanup(proxy.stop)
        client = httpx.Client(trust_env=False, timeout=10)
        self.addCleanup(client.close)
        response = client.post(proxy.base_url + "/chat/completions", json={
            "model": "pi-model", "stream": True,
            "messages": [{"role": "user", "content": "hello there"}]})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(rpc.settled.wait(5))
        self.assertGreaterEqual(len(rpc.artifacts), 2)
        request_artifact = json.loads(rpc.artifacts[0])
        self.assertEqual(request_artifact["retention"], "metadata_only")
        self.assertEqual(request_artifact["kind"], "model_request")
        response_artifact = json.loads(rpc.artifacts[1])
        self.assertEqual(response_artifact["retention"], "metadata_only")
        self.assertEqual(response_artifact["kind"], "model_response")
        self.assertGreater(budget.used_bytes, 0)
        self.assertGreater(budget.metadata_bytes, 0)
        self.assertGreaterEqual(budget.retained_count, 2)
        self.assertEqual(len([item for item in retained
                              if item.get("event") == "pi_artifact_retention"]), 1)
        # Retention metadata never carries credentials or harness secrets.
        artifact_corpus = "\n".join(rpc.artifacts).lower()
        for forbidden in ("authorization", "bearer", "harness_runner_token",
                          "api_key", "sk-"):
            self.assertNotIn(forbidden, artifact_corpus)
        # The valid artifact references still reach the ledger.
        request_event = next(event for event in rpc.events
                             if event["kind"] == "model_request")
        response_event = next(event for event in rpc.events
                              if event["kind"] == "model_response")
        self.assertTrue(request_event["payload"]["request_ref"]["hash"])
        self.assertTrue(response_event["payload"]["response_ref"]["hash"])

    def test_runner_artifact_kinds_use_the_shared_budget(self):
        budget = ArtifactBudget(1)
        text = budgeted_artifact_text(budget, "tool_result", "x" * 100)
        self.assertEqual(json.loads(text)["kind"], "tool_result")
        self.assertEqual(json.loads(text)["retention"], "metadata_only")



class BudgetPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state_path = self.root / "pi-artifact-budget.json"

    def test_usage_survives_a_simulated_restart(self):
        first = ArtifactBudget(1000, state_path=self.state_path)
        full = budgeted_artifact_text(first, "tool_result", "a" * 400)
        self.assertEqual(len(full), 400)
        fallback = budgeted_artifact_text(first, "model_response", "b" * 5000)
        self.assertEqual(json.loads(fallback)["retention"], "metadata_only")
        first.flush()
        used_before = first.used_bytes
        metadata_before = first.metadata_bytes
        self.assertGreater(metadata_before, 0)
        self.assertGreater(used_before, 400)
        restored = ArtifactBudget(1000, state_path=self.state_path)
        self.assertEqual(restored.used_bytes, used_before)
        self.assertEqual(restored.metadata_bytes, metadata_before)
        self.assertEqual(restored.retained_count, 1)
        self.assertEqual(restored.state_status, "restored")
        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o600)

    def test_missing_marker_is_a_fresh_budget(self):
        budget = ArtifactBudget(1000, state_path=self.state_path)
        self.assertEqual(budget.used_bytes, 0)
        self.assertEqual(budget.state_status, "fresh")

    def test_corrupt_marker_is_conservative_metadata_only(self):
        self.state_path.write_text("not-json{", encoding="utf-8")
        budget = ArtifactBudget(1000, state_path=self.state_path)
        self.assertEqual(budget.used_bytes, 1000)
        self.assertTrue(budget.state_status.startswith("conservative"))
        text = budgeted_artifact_text(budget, "model_request", "x" * 100)
        self.assertEqual(json.loads(text)["retention"], "metadata_only")
        self.assertTrue(budget.snapshot()["pressure_recorded"])

    def test_pressure_event_fires_once_at_ninety_percent_and_persists(self):
        events: list[dict] = []
        budget = ArtifactBudget(1000, event=events.append, state_path=self.state_path)
        budget.take(900)
        pressure = [item for item in events if item.get("event") == "pi_retention_pressure"]
        self.assertEqual(len(pressure), 1)
        self.assertEqual(pressure[0]["kind"], "artifact")
        self.assertEqual(pressure[0]["used_bytes"], 900)
        self.assertEqual(pressure[0]["percent"], 90.0)
        budget.take(10)
        self.assertEqual(len([item for item in events
                              if item.get("event") == "pi_retention_pressure"]), 1)
        budget.flush()
        restored = ArtifactBudget(1000, state_path=self.state_path)
        self.assertTrue(restored.snapshot()["pressure_recorded"])


class SessionPressureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_near_cap_session_emits_one_pressure_event(self):
        prepared = _fake_prepared(self.root, session_max=1000)
        _write_session(prepared.paths.session_dir / "active.jsonl", 950, age_seconds=1)
        stop_event = asyncio.Event()
        state: dict = {}
        self.assertFalse(pi_runner._check_active_session_limit(prepared, state, stop_event))
        self.assertFalse(stop_event.is_set())
        pressure = [payload for kind, payload in _fake_prepared.events
                    if kind == "pi_retention_pressure"]
        self.assertEqual(len(pressure), 1)
        self.assertEqual(pressure[0]["kind"], "session")
        self.assertEqual(pressure[0]["used_bytes"], 950)
        pi_runner._check_active_session_limit(prepared, state, stop_event)
        pressure = [payload for kind, payload in _fake_prepared.events
                    if kind == "pi_retention_pressure"]
        self.assertEqual(len(pressure), 1)

    def test_startup_near_cap_retention_warns(self):
        client = _FakeClient()
        result = pi_runner.SessionRetentionResult(
            total_before=950, total_after=950, pruned=[], archive_pruned=[],
            rotated=False, archived=None, resume=True)
        pi_runner._record_session_retention(client, "player1", result, max_bytes=1000)
        self.assertIn("pi_retention_pressure",
                      [kind for kind, _payload in client.events])


class ShutdownRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_player_shutdown_prunes_sessions_and_flushes_budget(self):
        prepared = _fake_prepared(self.root, session_max=500)
        prepared.spec.state_root = self.root
        prepared.client = _FakeClient()
        prepared.artifact_budget = ArtifactBudget(
            1000, state_path=self.root / "pi-artifact-budget.json")
        prepared.artifact_budget.take(100)
        for index in range(4):
            _write_session(prepared.paths.session_dir / f"session-{index}.jsonl", 300,
                           age_seconds=100 - index * 10)
        pi_runner._apply_shutdown_retention(prepared)
        total = sum(path.stat().st_size
                    for path in prepared.paths.session_dir.rglob("*.jsonl"))
        self.assertLessEqual(total, 500)
        kinds = [kind for kind, _payload in prepared.client.events]
        self.assertIn("pi_session_pruned", kinds)
        self.assertTrue((self.root / "pi-artifact-budget.json").is_file())
        restored = ArtifactBudget(1000, state_path=self.root / "pi-artifact-budget.json")
        self.assertEqual(restored.used_bytes, 100)


class PaperCapTests(unittest.TestCase):
    def test_paper_runner_caps_leave_run_wide_headroom(self):
        import yaml

        path = REPO / "configs" / "minecraft-paper-runner-20261022.yaml"
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        storage = config["storage"]
        artifacts = storage["pi_artifact_max_bytes"]
        sessions = storage["pi_session_max_bytes"]
        archive = pi_runner.PI_SESSION_ARCHIVE_MAX_BYTES
        max_run = storage["max_run_bytes"]
        self.assertEqual(artifacts, 6_000_000_000)
        self.assertEqual(sessions, 1_000_000_000)
        self.assertEqual(archive, 1_000_000_000)
        agents = 5  # 4 players + Observer
        total = agents * (artifacts + sessions + archive)
        self.assertLess(total, max_run)
        self.assertGreaterEqual(max_run - total, 10 * 1024 ** 3)  # ledger headroom


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Trusted control-plane artifact budget (P1-5 fix)
# ---------------------------------------------------------------------------

from minecraft_harness.recorder import Recorder  # noqa: E402
from minecraft_harness.runner_rpc import RpcClient, RpcServer  # noqa: E402
from minecraft_harness.runners import (RunnerManager,  # noqa: E402
                                       ServiceHub)

_CENTRAL_TEST_MODEL = {
    "kind": "chat_completions", "model": "fixture-model",
    "base_url": "https://api.example.test/v1", "api_key_env": "FAKE_API_KEY",
    "max_output_tokens": 4096, "timeout_s": 90,
}
_PI_CLI_RELATIVE = Path("pi_runtime") / "node_modules" / "@earendil-works" / \
    "pi-coding-agent" / "dist" / "bundle" / "cli.js"


def _central_config(*, per_agent: int = 2000, global_cap=None, agents=("player1",)):
    storage = {"pi_artifact_max_bytes": per_agent}
    if global_cap is not None:
        storage["pi_artifact_global_max_bytes"] = global_cap
    return validate_config({"agents": {"count": len(agents), "ids": list(agents)},
                            "storage": storage})


def _central_hub(run_dir: Path, config, recorder=None) -> ServiceHub:
    recorder = recorder or Recorder(run_dir)
    return ServiceHub(recorder=recorder, budget=None, memory=None,
                      gateway=SimpleNamespace(world_epoch=0), workspaces={},
                      artifacts=None, config=config)


class CentralBudgetHubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run_dir = self.root / "run"
        self.config = _central_config(per_agent=2000, global_cap=3000,
                                      agents=("player1", "player2"))
        self.recorder = Recorder(self.run_dir)
        self.addCleanup(self.recorder.close)
        self.hub = _central_hub(self.run_dir, self.config, self.recorder)

    async def take(self, agent, size, hub=None):
        return await (hub or self.hub).handle(
            agent, "player", "pi.budget.take", {"bytes": size})

    async def snapshot(self, agent, hub=None):
        return await (hub or self.hub).handle(
            agent, "player", "pi.budget.snapshot", {})

    async def test_per_agent_cap_and_metadata_charge(self):
        response = await self.take("player1", 1800)
        self.assertTrue(response["allowed"])
        self.assertEqual(response["used_bytes"], 1800)
        self.assertIsNotNone(response["pressure"])  # crosses 90% of 2000
        self.assertEqual(response["pressure"]["event"], "pi_retention_pressure")
        denied = await self.take("player1", 400)
        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["used_bytes"], 1800)
        self.assertEqual(denied["retained_count"], 1)
        self.assertEqual(denied["retention"]["event"], "pi_artifact_retention")
        self.assertEqual(denied["retention"]["reason"], "pi_artifact_max_bytes")
        charged = await self.hub.handle("player1", "player",
                                        "pi.budget.charge_metadata", {"bytes": 50})
        self.assertEqual(charged["used_bytes"], 1850)
        self.assertEqual(charged["metadata_bytes"], 50)
        snap = await self.snapshot("player1")
        self.assertEqual(snap["self"]["used_bytes"], 1850)
        self.assertEqual(snap["self"]["metadata_bytes"], 50)
        self.assertEqual(snap["self"]["retained_count"], 1)
        self.assertEqual(snap["limits"]["per_agent_bytes"], 2000)
        self.assertEqual(snap["limits"]["global_bytes"], 3000)

    async def test_global_cap_blocks_across_agents(self):
        self.assertTrue((await self.take("player1", 1800))["allowed"])
        denied = await self.take("player2", 1300)
        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["global_used_bytes"], 1800)
        self.assertEqual(denied["retention"]["reason"],
                         "pi_artifact_global_max_bytes")
        snap = await self.snapshot("player2")
        self.assertEqual(snap["self"]["used_bytes"], 0)
        self.assertEqual(snap["global"]["used_bytes"], 1800)
        self.assertEqual(snap["global"]["limit_bytes"], 3000)

    async def test_pressure_event_fires_once(self):
        first = await self.take("player1", 1800)
        self.assertIsNotNone(first["pressure"])
        second = await self.take("player1", 100)
        self.assertTrue(second["allowed"])
        self.assertIsNone(second.get("pressure"))
        snap = await self.snapshot("player1")
        self.assertTrue(snap["self"]["pressure_recorded"])

    async def test_concurrent_takes_never_exceed_the_cap(self):
        responses = await asyncio.gather(
            *[self.take("player1", 100) for _ in range(30)])
        allowed = [item for item in responses if item["allowed"]]
        self.assertEqual(len(allowed), 20)
        snap = await self.snapshot("player1")
        self.assertEqual(snap["self"]["used_bytes"], 2000)
        self.assertEqual(snap["self"]["retained_count"], 10)

    async def test_central_state_survives_a_servicehub_restart(self):
        self.assertTrue((await self.take("player1", 900))["allowed"])
        budget_path = self.hub._pi_budget_path
        self.assertEqual(budget_path.parent, self.run_dir)
        self.assertEqual(budget_path.name, "pi-budget.sqlite3")
        self.assertEqual(budget_path.stat().st_mode & 0o777, 0o600)
        restarted = _central_hub(self.run_dir, self.config, self.recorder)
        snap = await self.snapshot("player1", hub=restarted)
        self.assertEqual(snap["self"]["used_bytes"], 900)
        denied = await self.take("player1", 1200, hub=restarted)
        self.assertFalse(denied["allowed"])

    async def test_budget_methods_are_identity_bound(self):
        for agent, role in (("player1", "observer"), ("player2", "observer"),
                            ("nobody", "player"), ("observer", "player")):
            with self.subTest(agent=agent, role=role):
                with self.assertRaises(PermissionError):
                    await self.hub.handle(agent, role, "pi.budget.take",
                                          {"bytes": 1})
        observer_snapshot = await self.hub.handle(
            "observer", "observer", "pi.budget.snapshot", {})
        self.assertEqual(observer_snapshot["self"]["used_bytes"], 0)

    async def test_invalid_sizes_are_rejected(self):
        for size in (-1, "10", True, 2 ** 31 + 1):
            with self.subTest(size=size):
                with self.assertRaises(ValueError):
                    await self.hub.handle("player1", "player", "pi.budget.take",
                                          {"bytes": size})


class CentralArtifactBudgetRpcTests(unittest.IsolatedAsyncioTestCase):
    """ArtifactBudget over a real RPC server: a forged local marker is ignored."""

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run_dir = self.root / "run"
        self.config = _central_config(per_agent=2000, global_cap=5000)
        self.recorder = Recorder(self.run_dir)
        self.addCleanup(self.recorder.close)
        self.hub = _central_hub(self.run_dir, self.config, self.recorder)
        self.socket_path = self.root / "control.sock"
        self.tokens = {"token-player1": {"agent_id": "player1", "role": "player"},
                       "token-observer": {"agent_id": "observer", "role": "observer"}}
        self.server = RpcServer(self.socket_path, self.tokens, self.hub.handle)
        await self.server.start()
        self.addCleanup(self._close_server)
        self.client = RpcClient(self.socket_path, "token-player1", "player1", "player")
        self.observer_client = RpcClient(self.socket_path, "token-observer",
                                         "observer", "observer")
        self.marker = self.root / "state" / "pi-artifact-budget.json"
        self.marker.parent.mkdir(parents=True, exist_ok=True)

    async def _close_server(self):
        await self.server.close()

    async def make_budget(self, *, state_path=None, client=None, agent_id="player1",
                          max_bytes=2000):
        return await asyncio.to_thread(
            lambda: ArtifactBudget(max_bytes, rpc_client=client if client is not None
                                   else self.client, agent_id=agent_id,
                                   state_path=state_path))

    async def run_sync(self, function, *args):
        return await asyncio.to_thread(function, *args)

    def forged_marker(self, used_bytes=0):
        self.marker.write_text(json.dumps({
            "schema_version": 1, "limit_bytes": 2000, "used_bytes": used_bytes,
            "retained_count": 0, "metadata_bytes": 0,
            "retention_recorded": False, "pressure_recorded": False}),
            encoding="utf-8")

    async def test_forged_or_deleted_marker_cannot_lower_central_usage(self):
        self.assertTrue((await self.hub.handle(
            "player1", "player", "pi.budget.take", {"bytes": 600}))["allowed"])
        self.forged_marker(0)
        budget = await self.make_budget(state_path=self.marker)
        self.assertEqual(budget.used_bytes, 600)
        self.assertEqual(budget.state_status, "central")
        self.forged_marker(0)
        self.assertEqual((await self.make_budget(
            state_path=self.marker)).used_bytes, 600)
        self.marker.unlink()
        self.assertEqual((await self.make_budget(
            state_path=self.marker)).used_bytes, 600)

    async def test_restart_restores_central_usage_and_limits_takes(self):
        self.assertEqual((await self.make_budget()).used_bytes, 0)
        budget = await self.make_budget()
        self.assertTrue(await self.run_sync(budget.take, 1500))
        self.assertEqual(budget.used_bytes, 1500)
        # A fresh budget (a restarted runner) and a fresh ServiceHub both see it.
        restarted = await self.make_budget()
        self.assertEqual(restarted.used_bytes, 1500)
        self.assertFalse(await self.run_sync(restarted.take, 600))
        restarted_hub = _central_hub(self.run_dir, self.config, self.recorder)
        snap = await restarted_hub.handle("player1", "player",
                                          "pi.budget.snapshot", {})
        self.assertEqual(snap["self"]["used_bytes"], 1500)

    async def test_metadata_charges_are_central(self):
        budget = await self.make_budget()
        await self.run_sync(budget.charge_metadata, 80)
        snap = await self.hub.handle("player1", "player", "pi.budget.snapshot", {})
        self.assertEqual(snap["self"]["used_bytes"], 80)
        self.assertEqual(snap["self"]["metadata_bytes"], 80)

    async def test_central_pressure_event_is_emitted_once(self):
        budget = await self.make_budget()
        events: list[dict] = []
        budget._event = events.append
        self.assertTrue(await self.run_sync(budget.take, 1800))
        self.assertTrue(await self.run_sync(budget.take, 100))
        pressure = [item for item in events
                    if item.get("event") == "pi_retention_pressure"]
        self.assertEqual(len(pressure), 1)
        self.assertEqual(pressure[0]["limit_bytes"], 2000)

    async def test_observer_budget_uses_its_own_agent_row(self):
        observer = await self.make_budget(client=self.observer_client,
                                          agent_id="observer")
        self.assertTrue(await self.run_sync(observer.take, 700))
        snap = await self.hub.handle("observer", "observer",
                                     "pi.budget.snapshot", {})
        self.assertEqual(snap["self"]["used_bytes"], 700)
        player_snap = await self.hub.handle("player1", "player",
                                            "pi.budget.snapshot", {})
        self.assertEqual(player_snap["self"]["used_bytes"], 0)
        self.assertEqual(player_snap["global"]["used_bytes"], 700)

    async def test_central_unavailable_fails_closed(self):
        dead = RpcClient(self.root / "missing.sock", "token-player1",
                         "player1", "player")
        budget = await self.make_budget(client=dead)
        self.assertEqual(budget.used_bytes, 2000)
        self.assertEqual(budget.state_status, "conservative_central_unavailable")
        self.assertFalse(await self.run_sync(budget.take, 1))


class CentralBudgetIsolationTests(unittest.TestCase):
    """The trusted SQLite path is not reachable from a runner container."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run_dir = self.root / "run"
        source = self.root / "repo"
        (source / "src" / "minecraft_harness").mkdir(parents=True)
        (source / "src" / "minecraft_harness" / "__init__.py").write_text("")
        cli = source / _PI_CLI_RELATIVE
        cli.parent.mkdir(parents=True)
        cli.write_text("//cli")
        extensions = source / "pi_runtime" / "extensions"
        extensions.mkdir(parents=True)
        (extensions / "minecraft-tools.js").write_text("//")
        (extensions / "observer-tools.js").write_text("//")
        agents = ("player1",)
        config = {
            "runtime": {"mode": "fake", "runner_mode": "container",
                        "runner_runtime": "pi", "runner_egress": "bridge",
                        "pi_cli": str(cli), "pi_context_window": 32000,
                        "runner_container_shared_dir": str(self.root / "shared")},
            "model": dict(_CENTRAL_TEST_MODEL),
        }
        specs = {agent: {
            "schema_version": 1, "run_id": "run", "agent_id": agent,
            "role": "player", "state_root": str(self.run_dir / "agents" / agent),
            "shell_enabled": True, "heartbeat_s": 10,
            "agent_config": {"agent_id": agent, "goal": "g", "peers": [],
                             "profile": "paper_aligned"},
            "model": dict(_CENTRAL_TEST_MODEL),
            "pi_model": dict(_CENTRAL_TEST_MODEL),
            "cli_environment": {"MC_AGENT_ID": agent},
        } for agent in agents}
        tokens = {"token-player1": {"agent_id": "player1", "role": "player"}}
        self.manager = RunnerManager(
            run_dir=self.run_dir, config=config, specs=specs, token_map=tokens,
            service_handler=None, runner_env={"PATH": "/usr/bin:/bin"},
            docker_bin="docker", container_source_root=source,
            container_python="/usr/bin/python3.11")
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.manager.rpc_dir, True))

    def test_runner_container_mounts_do_not_include_the_budget_database(self):
        spec_path = self.run_dir / "runners" / "player1.json"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text("{}")
        state_dir = self.run_dir / "agents" / "player1"
        state_dir.mkdir(parents=True, exist_ok=True)
        arguments = self.manager._container_args("player1", spec_path, state_dir)
        command = " ".join(arguments)
        self.assertNotIn("pi-budget.sqlite3", command)
        self.assertNotIn(f"src={self.run_dir},", command)
        self.assertIn(f"src={state_dir},dst=/state", command)
        # The trusted authority lives beside the mounted state dir, not inside it.
        budget_path = self.run_dir / "pi-budget.sqlite3"
        self.assertNotEqual(budget_path.parent, state_dir)
