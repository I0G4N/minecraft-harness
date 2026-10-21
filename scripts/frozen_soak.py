"""Freeze application sources and read-only npm dependencies before an offline soak.

Default only prepares and verifies a snapshot. --execute is an explicit start.
No Minecraft server, external model, dependency install, or live-code import.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

DIRECTORIES=('src','bot_runtime/src','scripts')
FILES=('uv.lock','pyproject.toml','bot_runtime/package.json','bot_runtime/package-lock.json')


def digest_file(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def tree_hashes(root):
    root=Path(root).resolve();result={}
    for base,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=sorted(name for name in dirs if name!='__pycache__')
        for name in sorted(files+[name for name in dirs if (Path(base)/name).is_symlink()]):
            path=Path(base)/name
            if path.suffix=='.pyc':continue
            if path.is_symlink():
                if not path.resolve().is_relative_to(root):raise ValueError('dependency/source symlink escapes frozen tree: '+str(path))
                digest=hashlib.sha256(('symlink:'+os.readlink(path)).encode()).hexdigest()
            else:digest=digest_file(path)
            result[str(path.relative_to(root))]=digest
    return result


def source_hashes(root):
    result={}
    for relative in DIRECTORIES:
        result.update({relative+'/'+path:digest for path,digest in tree_hashes(root/relative).items()})
    result.update({relative:digest_file(root/relative) for relative in FILES})
    return dict(sorted(result.items()))


def tree_digest(hashes):
    return hashlib.sha256(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    descriptor,temporary=tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
    try:
        with os.fdopen(descriptor,'w') as stream:
            json.dump(value,stream,indent=2,sort_keys=True,allow_nan=False);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def readonly(root):
    for base,dirs,files in os.walk(root,topdown=False,followlinks=False):
        for name in files:
            path=Path(base)/name
            if not path.is_symlink():path.chmod(0o555 if path.stat().st_mode&0o111 else 0o444)
        for name in dirs:
            path=Path(base)/name
            if not path.is_symlink():path.chmod(0o555)
    Path(root).chmod(0o555)


BOOTSTRAP='''from pathlib import Path
import atexit,importlib,importlib.metadata,json,os,runpy,sys,tempfile
snapshot=Path(__file__).resolve().parents[1]
live=Path(os.environ['FROZEN_LIVE_ROOT']).resolve()
def blocked(path):
    target=Path(path or os.getcwd()).resolve()
    return target==live or any(target.is_relative_to(live/name) for name in ('src','scripts','bot_runtime'))
sys.path=[str(snapshot/'src')]+[path for path in sys.path if not blocked(path) and Path(path or os.getcwd()).resolve()!=snapshot/'src']
names=('minecraft_harness','minecraft_harness.supervisor','minecraft_harness.execution.gateway','minecraft_harness.observer.analysis','minecraft_harness.evaluation.collector')
modules={name:str(Path(importlib.import_module(name).__file__).resolve()) for name in names}
assert all(Path(path).is_relative_to(snapshot/'src') for path in modules.values()),modules
def audit():
    files={name:str(Path(module.__file__).resolve()) for name,module in sys.modules.items() if (name=='minecraft_harness' or name.startswith('minecraft_harness.')) and getattr(module,'__file__',None)}
    return {'modules':files,'all_application_imports_frozen':all(Path(path).is_relative_to(snapshot/'src') for path in files.values())}
if sys.argv[1:]==['--preflight']:
    gateway=Path(modules['minecraft_harness.execution.gateway'])
    print(json.dumps({**audit(),'gateway_node_path':str(gateway.parents[3]/'bot_runtime/src/server.js'),'sys_path_contains_live_source':any(blocked(path) for path in sys.path),'python':sys.version,'python_executable':sys.executable,'dependencies':{name:importlib.metadata.version(name) for name in ('httpx','PyYAML')}}))
else:
    def save_audit():
        path=Path(os.environ['FROZEN_IMPORT_AUDIT']);path.parent.mkdir(parents=True,exist_ok=True)
        descriptor,temporary=tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
        with os.fdopen(descriptor,'w') as stream:json.dump(audit(),stream,indent=2);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    atexit.register(save_audit)
    script=snapshot/'scripts/longrun_soak.py'
    sys.argv=[str(script),*sys.argv[1:]]
    runpy.run_path(str(script),run_name='__main__')
'''


def prepare(source,snapshot):
    source=Path(source).resolve();snapshot=Path(snapshot).absolute()
    if snapshot.exists():raise FileExistsError('use a new frozen snapshot directory')
    if any(snapshot.is_relative_to(source/relative) for relative in DIRECTORIES):raise ValueError('snapshot cannot be nested inside a copied source directory')
    before=source_hashes(source);dependency_before=tree_hashes(source/'bot_runtime/node_modules')
    if not dependency_before:raise ValueError('npm dependencies must already be installed')
    snapshot.parent.mkdir(parents=True,exist_ok=True)
    staging=snapshot.with_name('.'+snapshot.name+'.'+uuid.uuid4().hex)
    staging.mkdir()
    for relative in DIRECTORIES:
        shutil.copytree(source/relative,staging/relative,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for relative in FILES:
        (staging/relative).parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source/relative,staging/relative)
    shutil.copytree(source/'bot_runtime/node_modules',staging/'.dependencies/node_modules',symlinks=True)
    (staging/'bot_runtime/node_modules').symlink_to('../.dependencies/node_modules',target_is_directory=True)
    (staging/'.proof').mkdir();(staging/'.proof/bootstrap.py').write_text(BOOTSTRAP)
    copied=source_hashes(staging);dependency_copied=tree_hashes(staging/'.dependencies/node_modules')
    if before!=copied or before!=source_hashes(source):raise RuntimeError('application source changed during freeze')
    if dependency_before!=dependency_copied or dependency_before!=tree_hashes(source/'bot_runtime/node_modules'):raise RuntimeError('npm dependencies changed during freeze')
    manifest={'schema_version':1,'created_at':datetime.now(timezone.utc).isoformat(),'source_root':str(source),'snapshot_root':str(snapshot),
              'source_hashes':before,'source_tree_sha256':tree_digest(before),'dependency_hashes':dependency_copied,
              'dependency_tree_sha256':tree_digest(dependency_copied),'dependency_files':len(dependency_copied),
              'bootstrap_sha256':digest_file(staging/'.proof/bootstrap.py'),
              'dependency_policy':'copied private tree, mode read-only, linked as bot_runtime/node_modules; not a hostile same-UID security sandbox'}
    atomic_json(staging/'frozen-source.json',manifest);readonly(staging);os.replace(staging,snapshot)
    return manifest


def environment(snapshot,source):
    env=dict(os.environ)
    env.update(PYTHONPATH=str(snapshot/'src'),PYTHONDONTWRITEBYTECODE='1',FROZEN_LIVE_ROOT=str(source))
    for name in ('NODE_PATH','NODE_OPTIONS','PYTHONSTARTUP'):env.pop(name,None)
    return env


def preflight(snapshot,source):
    env=environment(snapshot,source)
    python=json.loads(subprocess.run([sys.executable,str(snapshot/'.proof/bootstrap.py'),'--preflight'],cwd=snapshot,env=env,check=True,capture_output=True,text=True,timeout=30).stdout)
    if python['gateway_node_path']!=str(snapshot/'bot_runtime/src/server.js') or python['sys_path_contains_live_source']:raise RuntimeError('Python/Node path preflight failed')
    script="""import {createRequire} from 'node:module';import {realpathSync} from 'node:fs';import {resolve} from 'node:path';const req=createRequire(resolve('package.json'));await import('./src/server.js');console.log(JSON.stringify({server:realpathSync('src/server.js'),mineflayer:realpathSync(req.resolve('mineflayer')),version:process.version,executable:process.execPath}));"""
    node=json.loads(subprocess.run(['node','--input-type=module','-e',script],cwd=snapshot/'bot_runtime',env=env,check=True,capture_output=True,text=True,timeout=30).stdout)
    if node['server']!=str(snapshot/'bot_runtime/src/server.js') or not Path(node['mineflayer']).is_relative_to(snapshot/'.dependencies/node_modules'):raise RuntimeError('Node resolved a live source/dependency')
    return {'python':python,'node':node}


def verify(snapshot,source,manifest):
    def matches(function,expected):
        try:return function()==expected
        except (OSError,ValueError):return False
    return {'snapshot_source_unchanged':matches(lambda:source_hashes(snapshot),manifest['source_hashes']),
            'original_source_unchanged':matches(lambda:source_hashes(source),manifest['source_hashes']),
            'snapshot_dependencies_unchanged':matches(lambda:tree_digest(tree_hashes(snapshot/'.dependencies/node_modules')),manifest['dependency_tree_sha256']),
            'original_dependencies_unchanged':matches(lambda:tree_digest(tree_hashes(source/'bot_runtime/node_modules')),manifest['dependency_tree_sha256']),
            'bootstrap_unchanged':matches(lambda:digest_file(snapshot/'.proof/bootstrap.py'),manifest['bootstrap_sha256'])}


def run(args,snapshot,source,manifest,paths):
    output=args.output.resolve();raw=output.with_name(output.stem+'.raw.json');log=output.with_suffix('.log');imports=output.with_name(output.stem+'.imports.json')
    if any(path.exists() for path in (output,raw,log,imports)):raise FileExistsError('preserve previous validation outputs; choose a new output name')
    output.parent.mkdir(parents=True,exist_ok=True)
    command=[sys.executable,str(snapshot/'.proof/bootstrap.py'),'--duration-s',str(args.duration_s),'--pace-s',str(args.pace_s),
             '--sample-interval-s',str(args.sample_interval_s),'--run-dir',str(args.run_dir.resolve()),'--output',str(raw)]
    if args.trace_memory:command.append('--trace-memory')
    env=environment(snapshot,source);env['FROZEN_IMPORT_AUDIT']=str(imports)
    beginning=verify(snapshot,source,manifest)
    if not all(beginning.values()):raise RuntimeError('freeze changed before start: '+str(beginning))
    print(json.dumps({'status':'starting','snapshot':str(snapshot),'command':command,'log':str(log),'output':str(output)},ensure_ascii=False),flush=True)
    interrupted=[];timed_out=False
    with log.open('x') as stream:
        process=subprocess.Popen(command,cwd=snapshot,env=env,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
        def forward(signum,_frame):
            interrupted.append(signum)
            if process.poll() is None:process.send_signal(signal.SIGINT)
        previous={signum:signal.signal(signum,forward) for signum in (signal.SIGINT,signal.SIGTERM)}
        try:
            try:code=process.wait(timeout=args.duration_s+300)
            except subprocess.TimeoutExpired:
                timed_out=True;process.send_signal(signal.SIGINT)
                try:code=process.wait(timeout=90)
                except subprocess.TimeoutExpired:process.kill();code=process.wait(timeout=15)
        finally:
            for signum,handler in previous.items():signal.signal(signum,handler)
    ending=verify(snapshot,source,manifest)
    import_audit=json.loads(imports.read_text()) if imports.exists() else {}
    summary=json.loads(raw.read_text()) if raw.exists() else {'status':'failed','checks':{},'failure':{'type':'MissingRawSummary','message':'child did not publish its result'}}
    freeze_checks={**ending,'child_exit_zero':code==0,'no_wrapper_timeout':not timed_out,'no_wrapper_interrupt':not interrupted,
                   'all_application_imports_frozen':import_audit.get('all_application_imports_frozen') is True}
    summary['checks'].update({'frozen_'+name:value for name,value in freeze_checks.items()})
    summary['status']='passed' if summary.get('status')=='passed' and all(freeze_checks.values()) else 'failed'
    summary['frozen_source']={'snapshot':str(snapshot),'manifest':str(snapshot/'frozen-source.json'),'manifest_sha256':digest_file(snapshot/'frozen-source.json'),
        'source_tree_sha256':manifest['source_tree_sha256'],'dependency_tree_sha256':manifest['dependency_tree_sha256'],
        'source_hashes':manifest['source_hashes'],'dependency_files':manifest['dependency_files'],'paths':paths,'checks_before':beginning,'checks_after':freeze_checks,
        'raw_summary':str(raw),'import_audit':str(imports),'log':str(log),'wrapper_pid':os.getpid(),'child_returncode':code,
        'scope':'strict offline fixture; child Python and Node memory measured by longrun_soak, preparation/monitor wrapper excluded'}
    atomic_json(output,summary)
    print(json.dumps({'status':summary['status'],'checks':summary['checks'],'output':str(output)},indent=2),flush=True)
    return 0 if summary['status']=='passed' else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--snapshot',type=Path)
    parser.add_argument('--run-dir',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--duration-s',type=float,default=3600)
    parser.add_argument('--pace-s',type=float,default=1)
    parser.add_argument('--sample-interval-s',type=float,default=5)
    parser.add_argument('--trace-memory',action='store_true')
    parser.add_argument('--execute',action='store_true',help='explicitly start the frozen offline soak after verification')
    parser.add_argument('--check-layout',action='store_true',help='verify path derivation only, without copying or launching')
    args=parser.parse_args();source=args.source_root.resolve();stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
    snapshot=(args.snapshot or source/'runs'/('frozen-source-'+stamp)).resolve()
    if args.check_layout:
        gateway=snapshot/'src/minecraft_harness/execution/gateway.py'
        print(json.dumps({'copied':False,'launched':False,'expected_python_root':str(snapshot/'src'),
                          'derived_node_root':str(gateway.parents[3]/'bot_runtime'),'node_root_matches':gateway.parents[3]/'bot_runtime'==snapshot/'bot_runtime'}));return 0
    args.run_dir=args.run_dir or source/'runs'/('paper-hour-frozen-'+stamp)
    args.output=args.output or source/'validation/2026-10-21-paper-hour-final.json'
    if not all(math.isfinite(value) and value>0 for value in (args.duration_s,args.pace_s,args.sample_interval_s)) or args.duration_s<10:parser.error('finite positive pace/sample interval and duration >=10 required')
    manifest=json.loads((snapshot/'frozen-source.json').read_text()) if snapshot.exists() else prepare(source,snapshot)
    if manifest['source_root']!=str(source) or manifest['snapshot_root']!=str(snapshot):raise ValueError('snapshot identity mismatch')
    paths=preflight(snapshot,source)
    if not args.execute:
        print(json.dumps({'status':'prepared_not_started','snapshot':str(snapshot),'paths':paths,'hash_checks':verify(snapshot,source,manifest)},indent=2));return 0
    return run(args,snapshot,source,manifest,paths)


if __name__=='__main__':raise SystemExit(main())
