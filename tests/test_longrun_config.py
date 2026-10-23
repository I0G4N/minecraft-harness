import asyncio
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

from minecraft_harness.config import load_config, validate_config
from minecraft_harness.ownership import RuntimeOwnership
from minecraft_harness.cli import _run_with_signals, _OperatorInterrupted, run_status
from minecraft_harness.recorder import Recorder, enqueue_control


class LongRunConfigurationTests(unittest.TestCase):
    def test_defaults_and_longrun_template_are_valid(self):
        config = validate_config({})
        self.assertEqual(config['supervision']['recovery_attempts'], 3)
        self.assertEqual(config['runtime']['lease_timeout_s'], 30)
        self.assertEqual(config['storage']['max_event_bytes'], 4 * 1024 ** 2)
        self.assertEqual(config['storage']['max_run_bytes'], 100 * 1024 ** 3)
        self.assertEqual(config['agents']['model_request_timeout_s'], 95)
        template = Path(__file__).resolve().parents[1] / 'configs/minecraft-longrun.yaml'
        loaded = load_config(template)
        self.assertEqual(loaded['budgets']['wall_time_minutes'], 1440)
        self.assertEqual(loaded['storage']['max_run_bytes'], 100 * 1024 ** 3)

    def test_pi_alignment_defaults_and_paper_runner_template(self):
        config = validate_config({})
        self.assertEqual(config['model']['max_request_bytes'], 4_000_000)
        self.assertIsNone(config['budgets']['max_web_calls'])
        self.assertEqual(config['supervision']['agent_failure_policy'], 'isolate')
        self.assertEqual(config['supervision']['observer_failure_policy'], 'degrade')
        self.assertEqual(config['runtime']['runner_rpc_timeout_s'], 120)
        template = load_config(Path(__file__).resolve().parents[1] /
                               'configs/minecraft-paper-runner-20261022.yaml')
        self.assertEqual(template['model']['max_request_bytes'], 4_000_000)
        self.assertEqual(template['budgets']['max_web_calls'], 2000)
        self.assertEqual(template['supervision']['agent_failure_policy'], 'isolate')
        self.assertEqual(template['supervision']['observer_failure_policy'], 'degrade')
        self.assertEqual(template['runtime']['runner_rpc_timeout_s'], 120)
        self.assertTrue(template['agents']['paper_single_program'])
        self.assertTrue(template['agents']['paper_tight_interface'])
        self.assertTrue(template['agents']['paper_shell_cli'])
        self.assertTrue(template['agents']['paper_cli_output'])

    def test_pi_alignment_limits_fail_before_start(self):
        invalid = [
            {'supervision': {'agent_failure_policy': 'continue'}},
            {'supervision': {'observer_failure_policy': 'ignore'}},
            {'budgets': {'max_web_calls': -1}},
            {'budgets': {'max_web_calls': True}},
            {'runtime': {'runner_rpc_timeout_s': 0}},
            # 4000 characters need 3*4000 + 64 KiB bytes; 70000 is below that.
            {'model': {'kind': 'chat_completions', 'base_url': 'https://example.test',
                       'model': 'm', 'max_request_bytes': 70000},
             'agents': {'context_chars': 4000}},
        ]
        for configuration in invalid:
            with self.subTest(configuration=configuration):
                with self.assertRaises(ValueError):
                    validate_config(configuration)

    def test_invalid_longrun_limits_fail_before_start(self):
        invalid = [
            {'agents': {'context_chars':3999}},
            {'agents': {'retry_base_s':10,'retry_max_s':1}},
            {'agents': {'max_model_failures':True}},
            {'agents': {'max_event_failures':0}},
            {'agents': {'max_workspace_bytes':0}},
            {'agents': {'ids':'player1'}},
            {'agents': {'count':1.0}},
            {'model': {'max_retries':1}},
            {'model': {'max_retries':False}},
            {'model': {'max_response_bytes':100}},
            {'model': {'kind':'chat_completions','base_url':'https://example.test','model':'m','max_request_bytes':100000}, 'agents': {'context_chars':200000}},
            {'model': {'kind':'chat_completions','base_url':'https://example.test','model':'m','max_request_bytes':100000}, 'observer': {'context_chars':200000}},
            {'observer': {'context_chars':3999}},
            {'storage': {'max_event_bytes':0}},
            {'storage': {'min_free_disk_bytes':-1}},
            {'storage': {'max_artifact_bytes':110*1024**3}},
            {'supervision': {'recovery_base_s':3,'recovery_max_s':1}},
            {'supervision': {'recovery_attempts':-1}},
            {'runtime': {'request_timeout_s':False}},
            {'runtime': {'heartbeat_interval_s':15,'lease_timeout_s':30}},
            {'runtime': {'urls': {'typo_agent':'http://localhost:3101'}}},
            {'server': {'managed_container':'$(malformed)'}},
            {'experiment': {'task': {'name':'obtain_logs','min_count':0}}},
        ]
        for value in (float('nan'),float('inf'),-float('inf')):
            invalid.extend({'supervision':{key:value}} for key in ('poll_interval_s','recovery_base_s','shutdown_timeout_s'))
            invalid.extend({'runtime':{key:value}} for key in ('request_timeout_s','heartbeat_interval_s','program_timeout_default_s'))
        for configuration in invalid:
            with self.subTest(configuration=configuration):
                with self.assertRaises(ValueError):
                    validate_config(configuration)

    def test_distinct_runtime_endpoints_cannot_share_managed_world_or_container(self):
        with tempfile.TemporaryDirectory() as directory:
            world = Path(directory) / 'world'
            world.mkdir()
            for common in ({'world_dir':str(world)}, {'managed_container':'longrun-test-' + str(os.getpid())}):
                first=RuntimeOwnership({'url':'http://localhost:23101'},common)
                second=RuntimeOwnership({'url':'http://localhost:23102'},common)
                first.acquire()
                try:
                    with self.assertRaisesRegex(RuntimeError,'already owned'):
                        second.acquire()
                    self.assertFalse(second.handles)
                finally:
                    first.close()
                second.acquire()
                second.close()

    def test_localhost_aliases_and_world_symlinks_do_not_bypass_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / 'data'
            (data / 'world').mkdir(parents=True)
            (root / 'alias').symlink_to(data, target_is_directory=True)
            pairs = [
                (RuntimeOwnership({'url':'http://127.0.0.1:23110/'}),RuntimeOwnership({'url':'http://LOCALHOST:23110'})),
                (RuntimeOwnership({}, {'world_dir':str(data/'world')}), RuntimeOwnership({}, {'world_dir':str(root/'alias/world')})),
            ]
            for first,second in pairs:
                first.acquire()
                try:
                    with self.assertRaises(RuntimeError):
                        second.acquire()
                finally:
                    first.close()
                    second.close()

    def test_status_exposes_control_ids_and_persisted_results(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder(directory)
            try:
                identity=enqueue_control(directory,'pause',{})
                report=run_status(directory)
                self.assertEqual(report['controls'][0]['control_id'],identity)
                self.assertEqual(report['control_counts'],{'queued':1})
                with recorder.db:
                    recorder.db.execute("UPDATE controls SET state='succeeded',result_json=? WHERE control_id=?",(json.dumps({'status':'paused'}),identity))
                self.assertEqual(run_status(directory)['controls'][0]['result'],{'status':'paused'})
            finally:
                recorder.close()


class SignalShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_sigterm_and_sigint_wait_for_supervisor_cleanup(self):
        for signum in (signal.SIGTERM,signal.SIGINT):
            callbacks={}
            cleaned=[]
            class SupervisorFixture:
                async def run(self):
                    try:
                        callbacks[signum]()
                        await asyncio.sleep(60)
                    finally:
                        await asyncio.sleep(.01)
                        cleaned.append(True)
            loop=asyncio.get_running_loop()
            def add_handler(number,callback,*arguments):
                callbacks[number]=lambda:callback(*arguments)
            with patch.object(loop,'add_signal_handler',side_effect=add_handler),patch.object(loop,'remove_signal_handler'):
                with self.assertRaises(_OperatorInterrupted) as raised:
                    await _run_with_signals(SupervisorFixture())
            self.assertEqual(raised.exception.signum,signum)
            self.assertEqual(cleaned,[True])
