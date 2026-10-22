#!/usr/bin/env python3
"""Fetch Mojang's official 1.20.4 server using its version manifest checksum."""

import hashlib
import json
from pathlib import Path
from urllib.request import urlopen


def main():
    version = "1.20.4"
    directory = Path(__file__).resolve().parent / ".cache"
    directory.mkdir(exist_ok=True)
    manifest_url = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
    with urlopen(manifest_url, timeout=60) as stream:
        manifest = json.load(stream)
    entry = next(item for item in manifest["versions"] if item["id"] == version)
    with urlopen(entry["url"], timeout=60) as stream:
        metadata_raw = stream.read()
    if hashlib.sha1(metadata_raw).hexdigest() != entry["sha1"]:
        raise RuntimeError("Version metadata checksum mismatch")
    download = json.loads(metadata_raw)["downloads"]["server"]
    path = directory / f"server-{version}.jar"
    if not path.is_file() or hashlib.sha1(path.read_bytes()).hexdigest() != download["sha1"]:
        with urlopen(download["url"], timeout=60) as stream:
            raw = stream.read()
        if len(raw) != download["size"] or hashlib.sha1(raw).hexdigest() != download["sha1"]:
            raise RuntimeError("Server jar checksum mismatch")
        temporary = path.with_suffix(".jar.tmp")
        temporary.write_bytes(raw)
        temporary.replace(path)
    lock = {"minecraft_version": version, "manifest_url": manifest_url,
            "metadata_url": entry["url"], "metadata_sha1": entry["sha1"],
            "server_url": download["url"], "server_sha1": download["sha1"],
            "server_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (directory / "server-lock.json").write_text(json.dumps(lock, indent=2) + "\n")
    print(json.dumps(lock, indent=2))


if __name__ == "__main__":
    main()
