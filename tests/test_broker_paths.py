"""The submission broker must reject over-long Unix socket paths up front."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from minecraft_harness.execution.broker import SubmissionBroker


class BrokerSocketPathTests(unittest.IsolatedAsyncioTestCase):
    def gateway(self, path):
        return SimpleNamespace(agent_ids=['alice'], config={'cli_broker_socket': str(path)})

    async def test_too_long_path_rejected_before_bind(self):
        path = Path('/tmp') / ('x' * 120) / 'socket'
        self.assertGreater(len(str(path).encode()), 107)
        broker = SubmissionBroker(self.gateway(path))
        with self.assertRaises(ValueError) as context:
            await broker.start()
        self.assertIn('too long', str(context.exception))

    async def test_short_path_binds_and_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'broker' / 'socket'
            broker = SubmissionBroker(self.gateway(path))
            await broker.start()
            try:
                self.assertTrue(path.is_socket())
                self.assertEqual(broker.url, 'http://localhost')
            finally:
                await broker.close()
            self.assertFalse(path.exists())
