"""Measure descriptor cleanup with real Node restarts; never forces GC or starts Minecraft."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path

from minecraft_harness.config import validate_config
from minecraft_harness.execution.gateway import ExecutionGateway
from minecraft_harness.recorder import Recorder, atomic_json


def fd_count():
    return len(os.listdir('/proc/self/fd'))


async def run(output):
    baseline = fd_count()
    lifecycle = []
    for _ in range(12):
        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder(directory)
            gateway = ExecutionGateway(validate_config({})['runtime'], recorder, ['alice', 'bob'], directory)
            try:
                await gateway.start()
                await gateway._stop_maintenance()
            finally:
                await gateway.close()
                recorder.close()
        # Allow ordinary transport callbacks; never invoke gc.collect().
        await asyncio.sleep(.02)
        lifecycle.append(fd_count())
    recovered = []
    with tempfile.TemporaryDirectory() as directory:
        recorder = Recorder(directory)
        config = validate_config({})['runtime']
        config.update(request_timeout_s=.5, heartbeat_interval_s=.1)
        gateway = ExecutionGateway(config, recorder, ['alice'], directory)
        try:
            await gateway.start()
            for cycle in range(8):
                request_id = f'fd-check-{cycle}'
                task = asyncio.create_task(gateway.execute('alice', 'while(true){}', timeout_ms=5000, request_id=request_id))
                for _ in range(100):
                    record = recorder.submission('alice', request_id)
                    if record and record.get('execution_id'):
                        break
                    await asyncio.sleep(.01)
                else:
                    raise RuntimeError('Program was not accepted before fault injection')
                gateway.process.kill()
                await gateway.process.wait()
                await task
                await gateway.recover(paused=False)
                await asyncio.sleep(.05)
                recovered.append(fd_count())
        finally:
            await gateway.close()
            recorder.close()
    await asyncio.sleep(.05)
    final = fd_count()
    source = Path(__file__).resolve().parents[1] / 'src/minecraft_harness/execution/gateway.py'
    passed = all(value == baseline for value in lifecycle) and max(recovered) - min(recovered) <= 4 and final == baseline
    result = {'kind': 'real_process_gateway_fd_cleanup', 'mode': 'fake', 'minecraft_server_started': False,
              'forced_gc': False, 'warnings_filtered': False, 'baseline_fds': baseline,
              'fds_after_each_start_close': lifecycle, 'fds_after_each_sigkill_recovery': recovered,
              'fds_after_final_close': final, 'gateway_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'status': 'passed' if passed else 'failed',
              'scope': 'Descriptor cleanup only. Does not assert that a complete isolated-loop test suite emits no ResourceWarning.'}
    atomic_json(output, result)
    print(json.dumps(result))
    if not passed:
        raise RuntimeError('Descriptor count did not remain bounded or return to baseline')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('validation/gateway-fd.json'))
    asyncio.run(run(parser.parse_args().output))
