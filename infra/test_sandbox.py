"""Native deny-write/exec probes, deliberately bypassing JavaScript allowlists."""
import json
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which('cc') and shutil.which('node') and os.name == 'posix', 'Linux C compiler and Node required')
class NativeSandboxTests(unittest.TestCase):
    def test_native_kernel_boundary_denies_mutation_processes_and_preserves_reads_threads(self):
        source = Path(__file__).with_name('sandbox.c')
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary) / 'sandbox.so'
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC', '-O2', '-o', str(library), str(source)], check=True, capture_output=True, timeout=20)
            script = r'''
const fs=require('fs'),cp=require('child_process'),{Worker}=require('worker_threads');
const target=process.argv[1];let write,asyncWrite,mkdir,rename;
try{fs.writeFileSync(target,'forbidden')}catch(e){write=e.code}
try{fs.mkdirSync(target+'dir')}catch(e){mkdir=e.code}
try{fs.renameSync(process.argv[2],target)}catch(e){rename=e.code}
(async()=>{
try{await fs.promises.writeFile(target,'forbidden')}catch(e){asyncWrite=e.code}
const read=fs.readFileSync(process.argv[2],'utf8').length>0;
const thread=await new Promise((resolve,reject)=>{const w=new Worker('require("worker_threads").parentPort.postMessage(42)',{eval:true});w.once('message',resolve);w.once('error',reject)});
console.log(JSON.stringify({write,asyncWrite,mkdir,rename,spawn:cp.spawnSync('/bin/true').error?.code,read,thread}));
})().catch(()=>process.exit(1));'''
            target = Path(temporary) / 'must-not-exist'
            env = {**os.environ, 'LD_PRELOAD': str(library)}
            result = subprocess.run(['node', '-e', script, str(target), str(source)], env=env, check=True, capture_output=True, text=True, timeout=15)
            self.assertEqual(json.loads(result.stdout), {'write': 'EPERM', 'asyncWrite': 'EPERM', 'mkdir': 'EPERM', 'rename': 'EPERM', 'spawn': 'EPERM', 'read': True, 'thread': 42})
            self.assertFalse(target.exists())
            self.assertFalse(Path(str(target) + 'dir').exists())
