"""Protocol-v3 asynchronous lifecycle and player CLI ledger integration."""
import argparse
import asyncio
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from minecraft_harness.config import validate_config
from minecraft_harness.execution.gateway import ExecutionGateway
from minecraft_harness.program_cli import invoke
from minecraft_harness.recorder import Recorder


@unittest.skipUnless(shutil.which('node'), 'Node required')
class PaperRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.recorder = Recorder(self.path)
        config = validate_config({})['runtime']
        config.update(lifecycle='paper_aligned', remaining_run_ms=20000, cli_broker_socket=str(self.path / 'broker' / 'rpc.sock'))
        self.gateway = ExecutionGateway(config, self.recorder, ['alice', 'bob'], self.path)
        await self.gateway.start()

    async def asyncTearDown(self):
        await self.gateway.close()
        self.recorder.close()
        self.temp.cleanup()

    async def test_submit_wait_expiry_does_not_cancel_or_hold_admission_lock(self):
        started = time.monotonic()
        first = await self.gateway.submit('alice', 'await bot.fake.wait(500);return 1', 2000, 'async-first')
        self.assertLess(time.monotonic() - started, .25)
        waiting = await self.gateway.wait('alice', first['execution_id'], 50)
        self.assertTrue(waiting['wait_expired'])
        self.assertIn(waiting['status'], {'accepted', 'running'})
        second = await self.gateway.submit('alice', 'return 2', 2000, 'async-second')
        self.assertNotEqual(first['execution_id'], second['execution_id'])
        duplicate = await self.gateway.submit('alice', 'await bot.fake.wait(500);return 1', 2000, 'async-first')
        self.assertEqual(duplicate['execution_id'], first['execution_id'])
        self.assertEqual((await self.gateway.wait('alice', first['execution_id'], 2000))['result'], 1)
        self.assertEqual((await self.gateway.wait('alice', second['execution_id'], 2000))['result'], 2)

    async def test_registered_job_survives_program_and_is_fenced_by_pause(self):
        first = await self.gateway.execute('alice', 'globals.n=0;return jobs.start(()=>{setInterval(()=>globals.n++,5)},{ttl_ms:4000})')
        await asyncio.sleep(.06)
        self.assertGreater((await self.gateway.execute('alice', 'return globals.n'))['result'], 0)
        tasks = await self.gateway.jobs('alice')
        self.assertEqual(tasks['jobs'][0]['job_id'], first['result'])
        await self.gateway.pause_sessions()
        self.assertEqual((await self.gateway.jobs('alice'))['jobs'][0]['status'], 'interrupted')
        await self.gateway.resume_sessions()
        self.assertEqual((await self.gateway.execute('alice', 'return typeof globals.n'))['result'], 'undefined')

    async def test_cli_unix_broker_shares_ledger_and_credentials_cannot_access_other_agent(self):
        source = self.path / 'action.js'
        source.write_text('globals.dispatches=(globals.dispatches??0)+1;return globals.dispatches')
        environment = self.gateway.cli_environment('alice')
        args = argparse.Namespace(url=None, agent=None, token_env='MC_AGENT_TOKEN', command='exec', file=source, request_id='cli-durable', timeout_ms=1000, wait_ms=1500)
        with patch.dict(os.environ, environment):
            first = await invoke(args)
            second = await invoke(args)
        self.assertEqual(first['execution_id'], second['execution_id'])
        self.assertEqual(second['result'], 1)
        persisted = self.recorder.submission('alice', 'cli-durable')
        self.assertEqual(persisted['execution_id'], first['execution_id'])
        self.assertEqual(persisted['request_sequence'], 1)
        transport = httpx.AsyncHTTPTransport(uds=environment['MC_RUNTIME_SOCKET'])
        async with httpx.AsyncClient(transport=transport, headers={'Authorization': 'Bearer ' + environment['MC_AGENT_TOKEN']}) as client:
            denied = await client.get('http://localhost/v1/sessions/bob')
            self.assertEqual(denied.status_code, 403)
            denied = await client.post('http://localhost/v1/sessions/alice/pause', json={})
            self.assertEqual(denied.status_code, 404)
        self.assertFalse(any(key in environment for key in ['MC_RUNTIME_TOKEN', 'OPENAI_API_KEY']))

    async def test_cli_paper_direct_runtime_is_refused_before_admission(self):
        source = self.path / 'action.js'; source.write_text('return 7')
        args = argparse.Namespace(url=self.gateway.url, agent='alice', token_env='RAW_TEST_TOKEN', command='exec', file=source, request_id='must-not-dispatch', timeout_ms=1000, wait_ms=1500)
        with patch.dict(os.environ, {'RAW_TEST_TOKEN': self.gateway.token}), self.assertRaisesRegex(ValueError, 'durable Supervisor'):
            await invoke(args)
        self.assertIsNone(self.recorder.submission('alice', 'must-not-dispatch'))

    async def test_terminal_observation_is_emitted_once_for_duplicate_execute_and_polls(self):
        first = await self.gateway.execute('alice', 'return 13', request_id='finish-once')
        await self.gateway.status('alice', first['execution_id'])
        await self.gateway.wait('alice', first['execution_id'], 10)
        await self.gateway.execute('alice', 'return 13', request_id='finish-once')
        events = [event for event in self.recorder.all_events() if event['kind'] == 'execution_finished' and event.get('execution_id') == first['execution_id']]
        self.assertEqual(len(events), 1)

    async def test_maintenance_stop_is_explicit_even_when_transport_consumes_cancel(self):
        await self.gateway._stop_maintenance()
        self.gateway.maintenance_stopping = False
        original = self.gateway._request
        reached = asyncio.Event()
        async def swallow_cancellation(method, path, body=None, agent_id=None, **kwargs):
            if path.endswith('/heartbeat'):
                reached.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    # Some nested cancellation scopes consume the request;
                    # the loop must still obey the independent stop signal.
                    return dict(self.gateway.sessions[agent_id])
            return await original(method, path, body, agent_id, **kwargs)
        self.gateway._request = swallow_cancellation
        for agent, session in self.gateway.sessions.items():
            self.gateway._ensure_tasks(agent, session)
        await asyncio.wait_for(reached.wait(), 2)
        await asyncio.wait_for(self.gateway._stop_maintenance(), 2)
        self.assertEqual(self.gateway.heartbeat_tasks, {})
        self.assertEqual(self.gateway.journal_tasks, {})
        self.gateway._request = original

    async def test_pause_exposes_lost_globals_epoch_before_any_new_program_or_old_chat_backlog(self):
        await self.gateway.execute('alice', 'globals.marker=1;return jobs.start(()=>{setInterval(()=>{},20)},{ttl_ms:5000})')
        old_epoch = self.gateway.sessions['alice']['runtime_epoch']
        for _ in range(250):
            self.recorder.append('player_event', {'kind': 'chat', 'world_epoch': 0, 'runtime_epoch': old_epoch, 'payload': ['someone', 'old queued chat']}, actor_id='alice')
        await self.gateway.pause_sessions()
        self.assertGreater(self.gateway.sessions['alice']['runtime_epoch'], old_epoch)
        await self.gateway.resume_sessions()
        events = await self.gateway.read_events('alice', 0, 1)
        self.assertGreater(events['runtime_epoch'], old_epoch)
        self.assertTrue(any(event['kind'] == 'chat' for event in events['events']))
        resumed = await self.gateway.execute('alice', 'return typeof globals.marker')
        self.assertEqual(resumed['runtime_epoch'], events['runtime_epoch'])
        self.assertEqual(resumed['result'], 'undefined')
