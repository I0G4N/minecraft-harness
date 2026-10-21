#!/usr/bin/env bash
# Install a pinned gVisor runsc release and register it with Docker.
# Requires root (or run with sudo). This is host-level configuration.
set -euo pipefail
VERSION="${GVSOR_VERSION:-20260608}"
ARCH="${GVSOR_ARCH:-$(uname -m)}"
case "$ARCH" in
  x86_64) ARCH=x86_64 ;;
  aarch64|arm64) ARCH=aarch64 ;;
  *) echo "unsupported architecture: $ARCH" >&2; exit 2 ;;
esac
URL="https://storage.googleapis.com/gvisor/releases/release/${VERSION}/${ARCH}/runsc"
if [[ $EUID -ne 0 ]]; then
  echo "run as root: sudo $0" >&2
  exit 2
fi
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
echo "downloading $URL"
curl -fL --retry 3 -o "$tmp/runsc" "$URL"
install -m 0755 "$tmp/runsc" /usr/local/bin/runsc
mkdir -p /etc/docker
python3 - <<'PY'
import json, pathlib
path = pathlib.Path('/etc/docker/daemon.json')
data = {}
if path.exists() and path.stat().st_size:
    data = json.loads(path.read_text())
data.setdefault('live-restore', True)
runtimes = data.setdefault('runtimes', {})
runtimes['runsc'] = {'path': '/usr/local/bin/runsc'}
runtimes.setdefault('runsc-debug', {'path': '/usr/local/bin/runsc'})
path.write_text(json.dumps(data, indent=2) + "\n")
PY
echo "installed /usr/local/bin/runsc ($VERSION)"
echo "restarting Docker to load the runtime..."
systemctl restart docker || service docker restart || true
for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
docker info --format 'Runtimes: {{range $k,$v := .Runtimes}}{{$k}} {{end}}'
echo "verify: docker run --rm --runtime=runsc --network none alpine uname -a"
