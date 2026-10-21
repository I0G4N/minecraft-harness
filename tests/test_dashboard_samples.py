import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from minecraft_harness.recorder import Recorder
from minecraft_harness.supervisor import Supervisor


class DurableDashboardSampleTests(unittest.IsolatedAsyncioTestCase):
    async def test_resource_history_survives_viewer_disconnect_and_preserves_unknown_cost(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            supervisor = Supervisor({'supervision': {'resource_check_interval_s': .01, 'resource_sample_interval_s': .02}}, run)
            supervisor.recorder = Recorder(run)
            supervisor.budget = SimpleNamespace(state={'calls': 3, 'input_tokens': 10, 'output_tokens': 2, 'cost_usd': None})
            supervisor.gateway = SimpleNamespace(world_epoch=7)
            task = asyncio.create_task(supervisor._resource_guard())
            try:
                for _ in range(100):
                    events = supervisor.recorder.read_events()
                    if sum(event['kind'] == 'resource_sample' for event in events) >= 2:
                        break
                    await asyncio.sleep(.01)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            samples = [event for event in supervisor.recorder.read_events() if event['kind'] == 'resource_sample']
            supervisor.recorder.close()
            self.assertGreaterEqual(len(samples), 2)
            self.assertTrue(all(event['world_epoch'] == 7 for event in samples))
            self.assertIsNone(samples[-1]['payload']['budget']['cost_usd'])
            self.assertEqual(samples[-1]['payload']['budget']['calls'], 3)
            self.assertIn('storage', samples[-1]['payload'])
            self.assertEqual(samples[-1]['payload']['measurement'], 'supervisor_python_rss_not_worker_or_server_memory')
