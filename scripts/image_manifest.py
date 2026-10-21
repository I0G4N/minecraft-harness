import hashlib,json,subprocess
from pathlib import Path
from datetime import datetime,timezone
root=Path(__file__).resolve().parents[1]
def command(*args):return subprocess.check_output(args,text=True).strip()
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
node_script="const fs=require('node:fs'),crypto=require('node:crypto');const h=crypto.createHash('sha256');for(const n of fs.readdirSync('/app/src').filter(n=>n.endsWith('.js')).sort())h.update(n).update(fs.readFileSync('/app/src/'+n));process.stdout.write(h.digest('hex'));"
h=hashlib.sha256()
files={}
for path in sorted((root/'bot_runtime/src').glob('*.js')):
 h.update(path.name.encode());h.update(path.read_bytes());files[str(path.relative_to(root))]=sha(path)
expected=h.hexdigest()
images={}
for role in ['control','worker','launcher','launcher-base','workspace']:
 tag={'launcher-base':'minecraft-harness-launcher-base:docker-cli-20261021','workspace':'minecraft-harness-workspace:local'}.get(role,f'minecraft-harness-{role}:research')
 inspection=json.loads(command('docker','image','inspect',tag))[0]
 record={'tag':tag,'image_id':inspection['Id'],'platform':inspection['Os']+'/'+inspection['Architecture'],'repo_digests':inspection.get('RepoDigests',[])}
 if role in ['control','worker','launcher']:
  digest=command('docker','run','--rm','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--entrypoint','node',tag,'-e',node_script)
  record['runtime_source_hash']=digest;record['matches_current_runtime_source']=digest==expected
  assert digest==expected,(role,digest,expected)
 if role=='worker':
  native_hash=command('docker','run','--rm','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--entrypoint','node',tag,'-e','const fs=require("node:fs"),c=require("node:crypto");process.stdout.write(c.createHash("sha256").update(fs.readFileSync("/opt/harness/sandbox.so")).digest("hex"));')
  record['native_filter_sha256']=native_hash
  record['native_filter_matches_host_binary']=native_hash==sha(root/'infra/.cache/sandbox.so')
  assert record['native_filter_matches_host_binary']
 if role=='workspace':
  cli_hash=command('docker','run','--rm','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--entrypoint','python3',tag,'-c','import hashlib;print(hashlib.sha256(open("/opt/mc.py","rb").read()).hexdigest())')
  record['cli_source_sha256']=cli_hash;record['matches_current_cli']=cli_hash==sha(root/'src/minecraft_harness/program_cli.py')
  assert record['matches_current_cli']
 images[role]=record
for path in ['infra/sandbox.c','infra/.cache/sandbox.so','infra/control.Dockerfile','infra/worker-sandbox.Dockerfile','infra/launcher.Dockerfile','infra/launcher-base.Dockerfile','infra/workspace.Dockerfile','bot_runtime/package-lock.json','src/minecraft_harness/execution/gateway.py','src/minecraft_harness/execution/broker.py','src/minecraft_harness/program_cli.py']:
 files[path]=sha(root/path)
result={'at':datetime.now(timezone.utc).isoformat(),'schema_version':1,'actual_minecraft':False,'paid_model':False,'runtime_source_hash':expected,'source_hash_algorithm':'sha256(concat(sorted JS basename UTF-8 + exact file bytes))','native_compile_command':'cc -Wall -Wextra -Werror -shared -fPIC -O2 -o infra/.cache/sandbox.so infra/sandbox.c','compiler':command('cc','--version').splitlines()[0],'images':images,'source_files_sha256':files,'launcher_base_provenance':'Existing Docker-CLI-equipped launcher 2af598098ffef2ea54771c7e75a6ff38e00c4b5c3ca64d74ff9c339f82c0c090 preserved as local immutable-content base; no package downloads during final rebuild.'}
(root/'validation/paper-images.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
print(json.dumps({'runtime_source_hash':expected,'images':{r:v['image_id'] for r,v in images.items()}},indent=2))
