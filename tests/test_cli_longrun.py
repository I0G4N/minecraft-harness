import asyncio
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from minecraft_harness.cli import doctor, main
from minecraft_harness.config import validate_config


class DoctorPreflightTests(unittest.TestCase):
    @staticmethod
    def config(count=1):
        return validate_config({'agents':{'count':count},'experiment':{'task':{'name':'observe_only'}},
            'runtime':{'mode':'minecraft','url':'http://runtime.test','token_env':'TEST_RPC_ADMIN'}})

    def run_with_transport(self, transport, config=None, timeout=5):
        original=httpx.AsyncClient
        def client(**kwargs):
            return original(transport=transport,**kwargs)
        with patch.dict(os.environ,{'TEST_RPC_ADMIN':'a-very-private-admin-token'}),patch('minecraft_harness.cli.httpx.AsyncClient',side_effect=client),patch('minecraft_harness.cli.DOCTOR_TIMEOUT_S',timeout):
            return doctor(config or self.config())

    def test_read_only_protocol_and_admin_checks_accept_dict_observe_task(self):
        requests=[]
        def respond(request):
            requests.append(request)
            if request.url.path=='/health':
                return httpx.Response(200,json={'status':'ok','protocol_version':2,'mode':'minecraft'})
            self.assertEqual(request.url.path,'/v1/sessions')
            self.assertEqual(request.headers['authorization'],'Bearer a-very-private-admin-token')
            return httpx.Response(200,json={'sessions':[]})
        result=self.run_with_transport(httpx.MockTransport(respond))
        self.assertTrue(result['ok'],result)
        self.assertEqual([request.method for request in requests],['GET','GET'])
        self.assertNotIn('a-very-private-admin-token',json.dumps(result))
        self.assertFalse(any(check['check']=='server world evidence configured' for check in result['checks']))

    def test_wrong_admin_token_is_rejected_without_echoing_secret_or_body(self):
        def respond(request):
            if request.url.path=='/health':
                return httpx.Response(200,json={'status':'ok','protocol_version':2,'mode':'minecraft'})
            return httpx.Response(403,text='a-very-private-admin-token private provider body')
        result=self.run_with_transport(httpx.MockTransport(respond))
        self.assertFalse(result['ok'])
        self.assertIn('403',json.dumps(result))
        self.assertNotIn('a-very-private-admin-token',json.dumps(result))
        self.assertNotIn('private provider body',json.dumps(result))

    def test_protocol_or_mode_mismatch_fails_preflight(self):
        for metadata in ({'status':'ok','protocol_version':1,'mode':'minecraft'},
                         {'status':'ok','protocol_version':2,'mode':'fake'},
                         {'status':'closing','protocol_version':2,'mode':'minecraft'}):
            with self.subTest(metadata=metadata):
                def respond(request):
                    return httpx.Response(200,json=metadata if request.url.path=='/health' else {'sessions':[]})
                self.assertFalse(self.run_with_transport(httpx.MockTransport(respond))['ok'])

    def test_global_deadline_bounds_all_agents_in_parallel(self):
        requests=[]
        async def hang(request):
            requests.append(request)
            await asyncio.sleep(60)
        started=time.monotonic()
        result=self.run_with_transport(httpx.MockTransport(hang),self.config(4),timeout=.05)
        self.assertLess(time.monotonic()-started,.5)
        self.assertEqual(len(requests),8)
        self.assertTrue(all(request.method=='GET' for request in requests))
        self.assertFalse(result['ok'])
        self.assertTrue(any('total deadline' in check.get('reason','') for check in result['checks']))

    def test_large_preflight_response_is_bounded(self):
        result=self.run_with_transport(httpx.MockTransport(lambda request:httpx.Response(200,content=b'x'*65537)))
        self.assertFalse(result['ok'])
        self.assertTrue(any('64 KiB' in check.get('reason','') for check in result['checks']))

    def test_fake_auto_runtime_does_not_make_http_requests(self):
        with patch('minecraft_harness.cli._runtime_doctor',side_effect=AssertionError('offline doctor accessed network')),patch('minecraft_harness.cli.shutil.which',return_value='/usr/bin/node'):
            self.assertTrue(doctor(validate_config({}))['ok'])


class CliExitStatusTests(unittest.TestCase):
    def test_run_and_resume_return_nonzero_for_all_infrastructure_failure_statuses(self):
        config=validate_config({})
        for command in ('run','resume'):
            for status in ('infra_failed','model_failed','reliability_failed','resource_exhausted'):
                with self.subTest(command=command,status=status),tempfile.TemporaryDirectory() as directory:
                    path=Path(directory)
                    (path/'manifest.json').write_text(json.dumps({'config':config}))
                    class SupervisorFixture:
                        async def run(self):
                            return {'status':status}
                    arguments=['run','--config','ignored.yaml'] if command=='run' else ['resume',str(path)]
                    with patch('minecraft_harness.cli.load_config',return_value=config),patch('minecraft_harness.cli.doctor',return_value={'ok':True}),patch('minecraft_harness.cli.is_active',return_value=False),patch('minecraft_harness.cli.Supervisor',return_value=SupervisorFixture()),redirect_stdout(io.StringIO()):
                        self.assertEqual(main(arguments),1)

    def test_batch_fails_if_any_trial_has_infrastructure_failure(self):
        config=validate_config({})
        with tempfile.TemporaryDirectory() as directory:
            statuses=iter(('diagnostic_pass','resource_exhausted'))
            class SupervisorFixture:
                def __init__(self):
                    self.status=next(statuses)
                async def run(self):
                    return {'status':self.status}
            with patch('minecraft_harness.cli.load_config',return_value=config),patch('minecraft_harness.cli.doctor',return_value={'ok':True}),patch('minecraft_harness.cli.Supervisor',side_effect=lambda *args,**kwargs:SupervisorFixture()),redirect_stdout(io.StringIO()):
                self.assertEqual(main(['batch','--config','ignored.yaml','--run-dir',str(Path(directory)/'batch'),'--seeds','42,43']),1)

    def test_failed_preflight_never_starts_supervisor_or_model(self):
        with patch('minecraft_harness.cli.load_config',return_value=validate_config({})),patch('minecraft_harness.cli.doctor',return_value={'ok':False,'checks':[{'check':'RPC network','ok':False}]}),patch('minecraft_harness.cli.Supervisor') as supervisor,redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            self.assertEqual(main(['run','--config','ignored.yaml']),2)
            supervisor.assert_not_called()


class DoctorCommandRegressionTests(unittest.TestCase):
    def test_actual_doctor_subcommand_without_run_dir_returns_json_and_creates_no_run(self):
        import shutil
        import subprocess
        import sys
        if not shutil.which('node'):
            self.skipTest('requires Node for offline doctor')
        config = Path(__file__).resolve().parents[1] / 'configs' / 'offline-smoke.yaml'
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-m', 'minecraft_harness', 'doctor', '--config', str(config)],
                                    cwd=directory, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)['ok'])
            self.assertNotIn('AttributeError', result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])
