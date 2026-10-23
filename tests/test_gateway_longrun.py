"""Real-process long-running transport failures, without Minecraft or model calls."""
import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from minecraft_harness.config import validate_config
from minecraft_harness.execution.gateway import ExecutionGateway, RuntimeReliabilityError, RuntimeUnavailable, _tagged_processes
from minecraft_harness.recorder import Recorder


@unittest.skipUnless(shutil.which('node') and Path('/proc').exists(), 'Linux and Node.js required')
class GatewayLongrunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.recorder = Recorder(self.path)
        self.gateways = []

    async def asyncTearDown(self):
        for gateway in reversed(self.gateways):
            try:
                await gateway.close()
            except RuntimeError:
                # Reliability-fault cases deliberately cannot certify the tail.
                if gateway.auto_spawn and gateway.runtime_instance_id:
                    await gateway._fence_owned_processes()
        self.recorder.close()
        self.temp.cleanup()

    async def gateway(self, agents=('alice',), **runtime):
        config = validate_config({})['runtime']
        config.update(runtime)
        gateway = ExecutionGateway(config, self.recorder, list(agents), self.path)
        self.gateways.append(gateway)
        await gateway.start()
        return gateway

    async def test_heartbeats_are_parallel_and_independent_of_slow_event_drain(self):
        gateway = await self.gateway(('alice', 'bob', 'carol', 'dave'), lease_timeout_s=1.0, heartbeat_interval_s=.1, request_timeout_s=2)
        original = gateway._request
        calls = {agent: 0 for agent in gateway.agent_ids}
        async def delayed(method, path, body=None, agent_id=None, **kwargs):
            if path.endswith('/heartbeat'):
                calls[agent_id] += 1
                if agent_id == 'alice':
                    await asyncio.sleep(1.4)
                    raise RuntimeUnavailable('one bot heartbeat endpoint is unavailable')
            if method == 'GET' and '/events?' in path:
                await asyncio.sleep(.7)
            return await original(method, path, body, agent_id, **kwargs)
        gateway._request = delayed
        await asyncio.sleep(1.5)
        for agent in ('bob', 'carol', 'dave'):
            state = await original('GET', f'/v1/sessions/{agent}', agent_id=agent)
            self.assertFalse(state['lease_expired'])
            self.assertGreaterEqual(calls[agent], 8)
        self.assertIsInstance(gateway.fault, RuntimeUnavailable)
        gateway._request = original

    async def test_lost_ack_response_retries_from_durable_cursor_without_duplicate_events(self):
        gateway = await self.gateway()
        await gateway._stop_maintenance()
        session = gateway.sessions['alice']
        original = gateway._request
        direct = await original('POST', '/v1/sessions/alice/programs', {**gateway._identity(session), 'request_id': 'external-ack', 'source': 'return 42', 'timeout_ms': 1000}, 'alice')
        await asyncio.sleep(.06)
        lost = False
        async def lose_ack(method, path, body=None, agent_id=None, **kwargs):
            nonlocal lost
            result = await original(method, path, body, agent_id, **kwargs)
            if path.endswith('/events/ack') and not lost:
                lost = True
                raise RuntimeUnavailable('ACK response lost after server pruned')
            return result
        gateway._request = lose_ack
        with self.assertRaises(RuntimeUnavailable):
            await gateway._drain('alice', session)
        committed = self.recorder.transport_cursor('alice', session['session_id'])
        self.assertGreater(committed, 0)
        node = await original('GET', '/v1/sessions/alice', agent_id='alice')
        self.assertEqual(node['acked_cursor'], committed)
        gateway._request = original
        await gateway._drain('alice', session)
        events = [event for event in self.recorder.all_events() if event['kind'] == 'execution_accepted' and event.get('execution_id') == direct['execution_id']]
        self.assertEqual(len(events), 1)
        self.assertEqual(self.recorder.execution('alice', direct['execution_id'])['result'], 42)

    async def test_two_phase_close_survives_lost_close_response(self):
        gateway = await self.gateway()
        await gateway._stop_maintenance()
        session = gateway.sessions['alice']
        direct = await gateway._request('POST', '/v1/sessions/alice/programs', {**gateway._identity(session), 'request_id': 'external-close', 'source': 'while(true){}', 'timeout_ms': 10000}, 'alice')
        original = gateway._request
        lost = False
        async def lose_close(method, path, body=None, agent_id=None, **kwargs):
            nonlocal lost
            result = await original(method, path, body, agent_id, **kwargs)
            if method == 'DELETE' and 'retain=true' in path and not lost:
                lost = True
                raise RuntimeUnavailable('DELETE response lost after worker was fenced')
            return result
        gateway._request = lose_close
        with self.assertRaises(RuntimeUnavailable):
            await gateway.fence()
        self.assertTrue((await original('GET', '/v1/sessions/alice', agent_id='alice'))['closed'])
        gateway._request = original
        await gateway.fence()
        self.assertEqual(self.recorder.execution('alice', direct['execution_id'])['status'], 'interrupted')
        self.assertNotIn('alice', gateway.sessions)

    async def test_node_sigkill_recovery_confirms_old_processes_absent_and_never_replays(self):
        gateway = await self.gateway(request_timeout_s=1, heartbeat_interval_s=.1)
        instance = gateway.runtime_instance_id
        task = asyncio.create_task(gateway.execute('alice', 'globals.old=true; while(true){}', timeout_ms=5000, request_id='never-replay'))
        for _ in range(100):
            record = self.recorder.submission('alice', 'never-replay')
            if record and record.get('execution_id'):
                break
            await asyncio.sleep(.01)
        self.assertTrue(record.get('execution_id'))
        gateway.process.kill()
        await gateway.process.wait()
        result = await asyncio.wait_for(task, 3)
        self.assertEqual(result['status'], 'unknown')
        await gateway.recover(paused=False)
        self.assertEqual(gateway.world_epoch, 1)
        self.assertIsNone(gateway.fault)
        self.assertEqual(await asyncio.to_thread(_tagged_processes, instance), [])
        old = self.recorder.submission('alice', 'never-replay')
        self.assertEqual(old['status'], 'interrupted')
        self.assertTrue(old['proven_old_worker_stopped'])
        repeated = await gateway.execute('alice', 'globals.old=true; while(true){}', timeout_ms=5000, request_id='never-replay')
        self.assertEqual(repeated['execution_id'], old['execution_id'])
        self.assertEqual(repeated['status'], 'interrupted')
        self.assertEqual((await gateway.execute('alice', 'return typeof globals.old'))['result'], 'undefined')

    async def test_new_gateway_cleans_durably_saved_previous_runtime_before_start(self):
        old = await self.gateway(heartbeat_interval_s=.1)
        await old._stop_maintenance()
        session = old.sessions['alice']
        # Reserve an uncertain operation exactly as a process dying before its
        # HTTP response would leave it in SQLite.
        pending, _ = self.recorder.reserve_submission('alice', 'abandoned', 'digest', {'status': 'unknown', **old._identity(session)})
        instance = old.runtime_instance_id
        self.assertTrue(await asyncio.to_thread(_tagged_processes, instance))
        await old.client.aclose()
        # A real Supervisor death closes its Python-owned listener through the
        # OS. This in-process crash fixture must reproduce that boundary while
        # deliberately leaving only the old Node process tree for recovery.
        listeners = tuple(old.cli_broker.server.sockets)
        await old.cli_broker.close()
        old.cli_broker = None
        self.assertTrue(all(listener.fileno() == -1 for listener in listeners))
        old.process_log.close()
        self.gateways.remove(old)  # Its owning Python process is conceptually gone.
        new = await self.gateway()
        self.assertNotEqual(new.runtime_instance_id, instance)
        self.assertEqual(await asyncio.to_thread(_tagged_processes, instance), [])
        self.assertEqual(self.recorder.submission('alice', 'abandoned')['status'], 'interrupted')
        owned = self.recorder.get_state('owned_runtime')
        self.assertEqual(owned['instance_id'], new.runtime_instance_id)
        self.assertNotIn('token', str(owned).lower())

    async def test_external_unreachable_runtime_is_not_blindly_recreated(self):
        config = validate_config({})['runtime']
        config['url'] = 'http://127.0.0.1:9'
        gateway = ExecutionGateway(config, self.recorder, ['alice'], self.path)
        self.gateways.append(gateway)
        async def unavailable(*args, **kwargs):
            raise RuntimeUnavailable('external runtime unavailable')
        gateway._request = unavailable
        with self.assertRaises(RuntimeUnavailable):
            await gateway.recover()
        self.assertEqual(gateway.world_epoch, 0)
        self.assertIsNone(gateway.process)
        self.gateways.remove(gateway)
        await gateway.close(fence=False)

    async def test_transient_event_read_is_retryable_but_overflow_is_critical(self):
        gateway = await self.gateway()
        await gateway._stop_maintenance()
        original = gateway._request
        async def unavailable(*args, **kwargs):
            raise httpx.ConnectError('temporary failure')
        gateway._request = unavailable
        with self.assertRaises(RuntimeUnavailable) as caught:
            await gateway.read_events('alice')
        self.assertTrue(caught.exception.retryable)
        gateway.fault = None
        gateway._request = original
        await gateway.read_events('alice')
        gateway._set_fault(RuntimeReliabilityError('journal overflow'))
        gateway._set_fault(RuntimeUnavailable('later network error'))
        self.assertIsInstance(gateway.fault, RuntimeReliabilityError)
        with self.assertRaises(RuntimeReliabilityError):
            await gateway.recover()
        gateway.fault = None

    async def test_expired_session_can_be_fenced_and_acked_after_worker_exit(self):
        gateway = await self.gateway(lease_timeout_s=.4, heartbeat_interval_s=.05)
        # Externally managed runtimes retain their audit tail after lease expiry.
        # Disable only the local self-exit behavior through a direct runtime start.
        await gateway._stop_maintenance()
        session = gateway.sessions['alice']
        original = gateway._request
        async def expired_ack(method, path, body=None, agent_id=None, **kwargs):
            result = await original(method, path, body, agent_id, **kwargs)
            if path.endswith('/events/ack'):
                result['lease_expired'] = True
            return result
        gateway._request = expired_ack
        await gateway.fence()
        self.assertEqual(gateway.sessions, {})

    async def test_operator_pause_rejection_does_not_trigger_runtime_recovery(self):
        gateway = await self.gateway()
        await gateway.pause_sessions()
        # A submit already admitted by Python can race the administrative pause.
        rejected = await gateway.execute('alice', 'return 1', request_id='paused-race')
        self.assertEqual(rejected['status'], 'rejected')
        self.assertIsNone(gateway.fault)
        self.assertEqual(gateway.world_epoch, 0)
        await gateway.resume_sessions()
        self.assertEqual((await gateway.execute('alice', 'return 2'))['result'], 2)

    async def test_invalid_player_event_limits_cannot_disable_sqlite_pagination(self):
        gateway = await self.gateway()
        for cursor, limit in [(-1, 10), (2**63, 10), (0, -1), (0, 0), (False, 10), (0, True)]:
            with self.assertRaises(ValueError):
                await gateway.read_events('alice', cursor, limit)
        self.assertIsNone(gateway.fault)

    async def test_session_startup_uses_its_own_httpx_and_total_deadline(self):
        config = validate_config({})['runtime']
        config.update(url='http://runtime.fixture', request_timeout_s=.03, startup_timeout_s=.3)
        gateway = ExecutionGateway(config, self.recorder, ['alice'], self.path)
        gateway.token = 'startup-fixture-supervisor-token'
        session = {'session_id': 'startup-fixture-session', 'agent_id': 'alice', 'run_id': self.recorder.run_id,
                   'world_epoch': 0, 'lease_timeout_ms': 30000, 'paused': False, 'closed': False,
                   'event_cursor': 0, 'acked_cursor': 0}
        creation_timeouts = []
        async def transport(request):
            if request.url.path == '/health':
                return httpx.Response(200, json={'protocol_version': 2, 'mode': 'fake'})
            if request.url.path == '/ordinary-slow':
                await asyncio.sleep(.12)
                return httpx.Response(200, json={})
            if request.method == 'POST' and request.url.path == '/v1/sessions':
                creation_timeouts.append(request.extensions['timeout']['read'])
                await asyncio.sleep(.12)
                return httpx.Response(201, json=session)
            if request.method == 'DELETE':
                return httpx.Response(200, json={**session, 'closed': True, 'deleted': 'retain=true' not in str(request.url),
                    'confirmed_old_process_exit': True, 'events': [], 'next_cursor': 0, 'has_more': False})
            if request.method == 'GET' and request.url.path.endswith('/events'):
                return httpx.Response(200, json={**session, 'events': [], 'next_cursor': 0, 'has_more': False})
            return httpx.Response(200, json=session)
        await gateway.client.aclose()
        gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False)
        self.gateways.append(gateway)
        with self.assertRaises(RuntimeUnavailable):
            await gateway._request('GET', '/ordinary-slow')
        await gateway.start()
        self.assertEqual(creation_timeouts, [.3])
        self.assertIn('alice', gateway.sessions)

    async def test_event_metadata_exposes_current_epoch_despite_old_chat_backlog(self):
        gateway = await self.gateway()
        first = gateway.sessions['alice']['runtime_epoch']
        for index in range(250):
            self.recorder.append('player_event', {'kind': 'chat', 'world_epoch': 0, 'runtime_epoch': first,
                'payload': ['someone', str(index)]}, actor_id='alice')
        await gateway.execute('alice', 'while(true){}', timeout_ms=30)
        await gateway.execute('alice', 'return true')
        feed = await gateway.read_events('alice', limit=1)
        self.assertEqual(len(feed['events']), 1)
        self.assertEqual(feed['events'][0]['kind'], 'chat')
        self.assertEqual(feed['world_epoch'], gateway.world_epoch)
        self.assertGreater(feed['runtime_epoch'], first)
        self.assertEqual(feed['session_id'], gateway.sessions['alice']['session_id'])

    async def test_shutdown_retries_remote_session_delete_timeout(self):
        """A slow sandbox teardown is retried instead of poisoning the run.

        Reproduces the 2026-10-23 15-minute run: player1 had an active
        280-second program at the wall deadline, the remote worker took longer
        than the 10 s request timeout to confirm exit, and the single DELETE
        attempt turned a completed run into infra_failed.  The second attempt
        must observe the closed session, commit its audit tail and succeed.
        """
        config = validate_config({})['runtime']
        config.update(url='http://runtime.fixture', request_timeout_s=.05,
                      close_request_timeout_s=.1, close_timeout_s=3,
                      startup_timeout_s=1, heartbeat_interval_s=.05, lease_timeout_s=1)
        gateway = ExecutionGateway(config, self.recorder, ['alice'], self.path)
        gateway.token = 'shutdown-fixture-supervisor-token'
        session = {'session_id': 'shutdown-fixture-session', 'agent_id': 'alice',
                   'run_id': self.recorder.run_id, 'world_epoch': 0,
                   'lease_timeout_ms': 1000, 'paused': False, 'closed': False,
                   'event_cursor': 0, 'acked_cursor': 0}
        state = {'deleted': False, 'retain_deletes': 0}

        async def transport(request):
            path, method = request.url.path, request.method
            if path == '/health':
                return httpx.Response(200, json={'protocol_version': 2, 'mode': 'fake'})
            if method == 'POST' and path == '/v1/sessions':
                return httpx.Response(201, json=session)
            if method == 'POST' and path.endswith('/heartbeat'):
                return httpx.Response(200, json=session)
            if method == 'POST' and path.endswith('/events/ack'):
                body = json.loads(request.content or b'{}')
                return httpx.Response(200, json={**session, 'acked_cursor': body.get('cursor', 0)})
            if method == 'GET' and path == '/v1/sessions/alice':
                return httpx.Response(404 if state['deleted'] else 200,
                                      json={} if state['deleted'] else session)
            if method == 'GET' and path.endswith('/events'):
                return httpx.Response(200, json={**session, 'events': [], 'next_cursor': 0, 'has_more': False})
            if method == 'DELETE':
                if 'retain=true' in str(request.url):
                    state['retain_deletes'] += 1
                    if state['retain_deletes'] == 1:
                        # Longer than close_request_timeout_s: the client
                        # times out while the runtime is still stopping the
                        # sandbox.  The retry must not be treated as a
                        # protocol contradiction.
                        await asyncio.sleep(.3)
                    return httpx.Response(200, json={**session, 'closed': True, 'deleted': False,
                        'confirmed_old_process_exit': True, 'events': [], 'next_cursor': 0, 'has_more': False})
                state['deleted'] = True
                return httpx.Response(200, json={**session, 'closed': True, 'deleted': True,
                    'confirmed_old_process_exit': True, 'events': []})
            return httpx.Response(404, json={'error': 'unexpected fixture route'})

        await gateway.client.aclose()
        gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False)
        self.gateways.append(gateway)
        await gateway.start()
        await gateway._stop_maintenance()
        await gateway.close()
        self.assertEqual(gateway.sessions, {})
        self.assertTrue(state['deleted'])
        self.assertGreaterEqual(state['retain_deletes'], 2)
        retries = [event for event in self.recorder.all_events()
                   if event['kind'] == 'runtime_shutdown_retry']
        self.assertGreaterEqual(len(retries), 1)
        self.assertEqual(retries[0]['payload']['agent_id'], 'alice')
