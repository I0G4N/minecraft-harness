import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from minecraft_harness.models import FakeModel, ModelResponse
from minecraft_harness.observer.agent import ObserverAgent
from minecraft_harness.recorder import Recorder
from test_paper_agents import Budget


class ObserverOperatorBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_own_bookkeeping_is_not_review_evidence_and_cursor_advances(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recorder = Recorder(root)
            self.addCleanup(recorder.close)
            observer = ObserverAgent({}, root, recorder, FakeModel(), Budget())
            for index in range(3):
                recorder.append("model_request", {"turn": index}, actor_id="observer")
            recorder.append("runtime_fault", {"reason": "game evidence"})
            page = observer._records_read(limit=100)
            self.assertEqual([event["kind"] for event in page["events"]], ["runtime_fault"])
            self.assertEqual(page["next_cursor"], recorder.latest_event_seq())
            self.assertFalse(page["truncated"])
            self.assertEqual(observer._records_read(after=page["next_cursor"], limit=100)["events"], [])

    async def test_page_bound_reports_truncation_without_losing_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recorder = Recorder(root)
            self.addCleanup(recorder.close)
            observer = ObserverAgent({}, root, recorder, FakeModel(), Budget())
            for index in range(12):
                recorder.append("runtime_fault", {"reason": f"{index}:" + "x" * 3000})
            cursor, reasons = 0, []
            for _ in range(20):
                page = observer._records_read(after=cursor, limit=100)
                reasons += [int(event["payload"]["reason"].split(":", 1)[0]) for event in page["events"]]
                cursor = page["next_cursor"]
                if not page["truncated"]:
                    break
            self.assertEqual(reasons, list(range(12)))

    async def test_busy_backlog_cannot_outpace_the_review_cadence(self):
        class ReportingModel:
            calls = 0
            async def complete(self, messages, schemas):
                self.calls += 1
                return ModelResponse(text="", tool_calls=[{
                    "id": f"review-{self.calls}", "type": "function",
                    "function": {"name": "observer_report", "arguments": json.dumps({"findings": []})}}],
                    usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recorder = Recorder(root)
            self.addCleanup(recorder.close)
            for index in range(300):
                recorder.append("runtime_fault", {"reason": f"{index}:" + "x" * 3000})
            model = ReportingModel()
            observer = ObserverAgent({"interval_s": 60, "max_calls_per_review": 4,
                                      "context_chars": 200000}, root, recorder, model, Budget())
            task = asyncio.create_task(observer.run())
            await asyncio.sleep(.5)
            observer.stop()
            await asyncio.wait_for(task, 5)
            # One review per interval: a backlog must not create a tight loop.
            self.assertEqual(model.calls, 1)

    async def test_one_review_ingests_bounded_evidence_pages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recorder = Recorder(root)
            self.addCleanup(recorder.close)
            observer = ObserverAgent({"context_chars": 200000}, root, recorder, FakeModel(), Budget())
            for index in range(40):
                recorder.append("runtime_fault", {"reason": f"{index}:" + "x" * 2000})
            page = observer._read_evidence()
            self.assertGreater(len(page["events"]), 1)
            self.assertLessEqual(sum(len(json.dumps(event, ensure_ascii=False)) for event in page["events"]),
                                 observer.evidence_bytes + observer.PAGE_BYTES)
            self.assertEqual([int(event["payload"]["reason"].split(":", 1)[0]) for event in page["events"]],
                             list(range(len(page["events"]))))

    async def test_review_with_new_evidence_is_pushed_once_to_publish_a_report(self):
        class SilentThenReportingModel:
            calls = 0
            choices = []
            async def complete(self, messages, schemas, tool_choice=None):
                self.calls += 1
                self.choices.append(tool_choice)
                if self.calls == 1:
                    return ModelResponse(text="I read the evidence.", usage={
                        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
                return ModelResponse(text="", tool_calls=[{
                    "id": "review", "type": "function",
                    "function": {"name": "observer_report", "arguments": json.dumps({"findings": []})}}],
                    usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recorder = Recorder(root)
            self.addCleanup(recorder.close)
            recorder.append("runtime_fault", {"reason": "game evidence"})
            model = SilentThenReportingModel()
            observer = ObserverAgent({"interval_s": 60}, root, recorder, model, Budget())
            task = asyncio.create_task(observer.run())
            await asyncio.sleep(.3)
            observer.stop()
            await asyncio.wait_for(task, 5)
            self.assertEqual(model.calls, 2)
            self.assertIsNone(model.choices[0])
            self.assertEqual(model.choices[1]["function"]["name"], "observer_report")
            self.assertTrue(any(event["kind"] == "observer_report" for event in recorder.all_events()))
            self.assertTrue(any(event["kind"] == "observer_review_missing_report" for event in recorder.all_events()))

    async def test_failed_forced_report_is_retried_once_with_error_feedback(self):
        class InvalidThenValidModel:
            calls = 0
            forced = 0
            retry_message = ""
            async def complete(self, messages, schemas, tool_choice=None):
                self.calls += 1
                if tool_choice is None:
                    return ModelResponse(text="I read the evidence.", usage={
                        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
                self.forced += 1
                if self.forced == 2:
                    self.retry_message = messages[-1].get("content") or ""
                refs = [999999] if self.forced == 1 else [1]
                return ModelResponse(text="", tool_calls=[{
                    "id": f"report-{self.calls}", "type": "function",
                    "function": {"name": "observer_report",
                                 "arguments": json.dumps({"findings": [{"status": "insufficient_evidence",
                                                                        "claim": "synthetic", "evidence_refs": refs}]})}}],
                    usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recorder = Recorder(root)
            self.addCleanup(recorder.close)
            recorder.append("runtime_fault", {"reason": "game evidence"})
            model = InvalidThenValidModel()
            observer = ObserverAgent({"interval_s": 60}, root, recorder, model, Budget())
            task = asyncio.create_task(observer.run())
            await asyncio.sleep(.3)
            observer.stop()
            await asyncio.wait_for(task, 5)
            self.assertEqual(model.forced, 2)
            self.assertIn("Allowed event_seq", model.retry_message)
            self.assertTrue(any(event["kind"] == "observer_report" for event in recorder.all_events()))

    async def test_operator_world_identity_never_reaches_observer_or_artifact_acl(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recorder = Recorder(root)
            self.addCleanup(recorder.close)
            observer = ObserverAgent({}, root, recorder, FakeModel(), Budget())
            reference = observer._artifact({"ordinary": "own artifact"})
            # Use a different artifact which has never been delivered to the Observer.
            private = recorder.put_artifact(json.dumps({"seed": 867530912345678}).encode())
            recorder.append("effective_environment_verified", {
                "actual": {"seed": 867530912345678}, "raw_ref": private})
            recorder.append("operator_world_archive", {"operator_only": True,
                "raw_ref": private, "seed": 867530912345678})
            recorder.append("runtime_fault", {"reason": "fixture"})
            page = observer._records_read()
            self.assertEqual(len(page["events"]), 3)
            self.assertNotIn("867530912345678", json.dumps(page))
            self.assertNotIn(private["hash"], json.dumps(page))
            self.assertEqual(page["events"][0]["payload"], {"operator_only": True, "redacted": True})
            self.assertEqual(page["events"][-1]["payload"]["reason"], "fixture")
            with self.assertRaises(PermissionError):
                observer._artifact_read(private["hash"])
            self.assertIn("own artifact", observer._artifact_read(reference["hash"])["content"])
