"""Operator-only complete data-directory snapshots for a stopped Minecraft server.

The caller must first fence all bot runtimes, stop the server, and exclude other
writers for the entire operation. This module does not run Docker, stop processes,
or claim that copying a live world is a consistent checkpoint. Checkpoints and
backups belong to the operator; never mount them into an agent workspace.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


MARKER = "minecraft-harness-stopped-server-checkpoint"
MANIFEST = "manifest.json"
SCHEMA_VERSION = 1
MIN_FREE_DISK_BYTES = 1024 ** 3


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=False):
        for name in files:
            with (Path(current) / name).open("rb") as stream:
                os.fsync(stream.fileno())
        _fsync_directory(Path(current))


def _require_space(target: Path, bytes_needed: int, minimum: int) -> None:
    if type(minimum) is not int or minimum < 0:
        raise ValueError("min_free_disk_bytes must be a nonnegative integer")
    parent = target
    while not parent.exists():
        parent = parent.parent
    if shutil.disk_usage(parent).free < bytes_needed + minimum:
        raise OSError(f"Insufficient free disk for checkpoint/restore: need {bytes_needed} bytes plus {minimum} bytes reserve")


def _journal_path(data: Path) -> Path:
    return data.parent / ("." + data.name + ".harness-restore.json")


def _receipt_path(data: Path) -> Path:
    return data.parent / ("." + data.name + ".harness-restore-receipt.json")


def has_interrupted_restore(data_dir: str | Path) -> bool:
    path = _journal_path(_path(data_dir))
    return path.exists() or path.is_symlink()


def _write_journal(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _clear_journal(path: Path) -> None:
    path.unlink()
    _fsync_directory(path.parent)


def _path(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    current = Path(raw.anchor)
    for component in raw.parts[1:]:
        current = current / component
        if current.is_symlink():
            raise ValueError(f"Symlink paths are not permitted: {current}")
    path = Path(os.path.abspath(raw))
    if path == Path(path.anchor):
        raise ValueError("A filesystem root cannot be a checkpoint/data/backup directory")
    return path


def _distinct(*paths: Path) -> None:
    for index, first in enumerate(paths):
        for second in paths[index + 1:]:
            if first.is_relative_to(second) or second.is_relative_to(first):
                raise ValueError("Data, checkpoint, and backup directories must not be equal or nested")


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("Invalid checkpoint manifest path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or value == ".":
        raise ValueError("Checkpoint manifest paths must be normalized relative paths")
    return value


def _scan(root: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Reject links/devices/FIFOs before any destructive operation."""
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Snapshot data must be a real directory")
    files: dict[str, dict[str, Any]] = {}
    directories: list[str] = []
    def walk_error(error: OSError) -> None:
        raise error

    for current, names, filenames in os.walk(root, followlinks=False, onerror=walk_error):
        for name in sorted(names + filenames):
            path = Path(current) / name
            metadata = path.lstat()
            relative = path.relative_to(root).as_posix()
            _relative(relative)
            if stat.S_ISDIR(metadata.st_mode):
                directories.append(relative)
            elif stat.S_ISREG(metadata.st_mode):
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                files[relative] = {"sha256": digest.hexdigest(), "size": metadata.st_size}
            else:
                raise ValueError(f"Symlinks and special files are not permitted: {relative}")
    return dict(sorted(files.items())), sorted(directories)


def _copy_tree(source: Path, destination: Path, files: dict, directories: list[str]) -> None:
    destination.mkdir()
    for relative in sorted(directories, key=lambda value: (value.count("/"), value)):
        (destination / relative).mkdir()
    for relative in files:
        path = source / relative
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("Source changed while snapshotting; exclusive access is required")
        shutil.copy2(path, destination / relative, follow_symlinks=False)


def _build(source: Path, target: Path, metadata: dict[str, Any], checkpoint_id: str,
           min_free_disk_bytes: int = MIN_FREE_DISK_BYTES) -> dict[str, Any]:
    if target.exists():
        raise FileExistsError("Checkpoint/backup target already exists")
    # Encode caller metadata before allocating output; no partial snapshot is
    # published if it is not serializable or contains non-finite numbers.
    if not isinstance(metadata, dict):
        raise ValueError("Checkpoint metadata must be an object")
    saved_metadata = json.loads(json.dumps(metadata, ensure_ascii=False, allow_nan=False))
    files, directories = _scan(source)
    _require_space(target, sum(entry["size"] for entry in files.values()), min_free_disk_bytes)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / (".pending-" + target.name + "-" + uuid.uuid4().hex)
    staging.mkdir()
    try:
        _copy_tree(source, staging / "data", files, directories)
        copied_files, copied_directories = _scan(staging / "data")
        if copied_files != files or copied_directories != directories:
            raise ValueError("Source changed while snapshotting; exclusive access is required")
        manifest = {"marker": MARKER, "schema_version": SCHEMA_VERSION,
                    "checkpoint_id": checkpoint_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "metadata": saved_metadata, "files": files, "directories": directories}
        _fsync_tree(staging / "data")
        with (staging / MANIFEST).open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(staging)
        staging.rename(target)
        _fsync_directory(target.parent)
        return manifest
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def create_checkpoint(data_dir: str | Path, checkpoints_dir: str | Path,
                      metadata: dict[str, Any], *, min_free_disk_bytes: int = MIN_FREE_DISK_BYTES) -> dict[str, Any]:
    """Create a new verified checkpoint under ``checkpoints_dir``.

    All files are copied, including server configuration and player statistics.
    The manifest preserves arbitrary JSON metadata such as world/runtime epochs.
    A failed snapshot is removed; the source directory is never modified.
    """
    data, checkpoints = _path(data_dir), _path(checkpoints_dir)
    _distinct(data, checkpoints)
    identifier = "checkpoint-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
    target = checkpoints / identifier
    if has_interrupted_restore(data):
        raise RuntimeError("Interrupted restore requires recovery before snapshotting")
    if read_pending_restore_receipt(data) is not None:
        raise RuntimeError("Pending restore receipt must be committed and acknowledged before snapshotting")
    manifest = _build(data, target, metadata, identifier, min_free_disk_bytes)
    return {**manifest, "checkpoint_dir": str(target)}


def _validated_checkpoint(checkpoint: Path) -> dict[str, Any]:
    if not checkpoint.is_dir() or checkpoint.is_symlink():
        raise ValueError("Checkpoint directory does not exist or is a symlink")
    if set(item.name for item in checkpoint.iterdir()) != {MANIFEST, "data"}:
        raise ValueError("Checkpoint must contain only its manifest and data directory")
    manifest_path = checkpoint / MANIFEST
    if not stat.S_ISREG(manifest_path.lstat().st_mode) or manifest_path.is_symlink():
        raise ValueError("Checkpoint manifest must be a regular file")

    def reject_constant(value: str) -> None:
        raise ValueError(f"Non-finite checkpoint manifest value: {value}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"),
                              parse_constant=reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Checkpoint manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("marker") != MARKER or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Only checkpoints created by this tool are accepted")
    if not isinstance(manifest.get("checkpoint_id"), str) or not isinstance(manifest.get("metadata"), dict):
        raise ValueError("Checkpoint identity or metadata is invalid")
    files, directories = manifest.get("files"), manifest.get("directories")
    if not isinstance(files, dict) or not isinstance(directories, list):
        raise ValueError("Checkpoint file manifest is invalid")
    for relative, expected in files.items():
        _relative(relative)
        if not isinstance(expected, dict) or set(expected) != {"sha256", "size"}:
            raise ValueError("Checkpoint file hash entry is invalid")
        if not isinstance(expected["sha256"], str) or len(expected["sha256"]) != 64 or type(expected["size"]) is not int or expected["size"] < 0:
            raise ValueError("Checkpoint file hash/size is invalid")
    for relative in directories:
        _relative(relative)
    actual_files, actual_directories = _scan(checkpoint / "data")
    if files != actual_files or sorted(directories) != actual_directories:
        raise ValueError("Checkpoint contents differ from their SHA256/size/path manifest")
    return manifest


def _empty_data_root(root: Path) -> None:
    # Keep root itself (and hence an existing Docker bind mount) intact. Never
    # follow a link even if an operator creates one after the initial validation.
    for child in root.iterdir():
        if child.is_symlink():
            raise ValueError("Data directory changed during restore; symlink encountered")
        if child.is_dir():
            shutil.rmtree(child)
        elif stat.S_ISREG(child.lstat().st_mode):
            child.unlink()
        else:
            raise ValueError("Data directory changed during restore; special file encountered")


def _restore_contents(checkpoint: Path, destination: Path, manifest: dict[str, Any]) -> None:
    _empty_data_root(destination)
    source = checkpoint / "data"
    for relative in sorted(manifest["directories"], key=lambda value: (value.count("/"), value)):
        (destination / relative).mkdir()
    for relative in manifest["files"]:
        shutil.copy2(source / relative, destination / relative, follow_symlinks=False)
    actual_files, actual_directories = _scan(destination)
    if actual_files != manifest["files"] or actual_directories != sorted(manifest["directories"]):
        raise ValueError("Restored world failed content verification")
    _fsync_tree(destination)


def restore_checkpoint(checkpoint_dir: str | Path, data_dir: str | Path,
                       backup_dir: str | Path, *, min_free_disk_bytes: int = MIN_FREE_DISK_BYTES,
                       expected_run_id: str | None = None) -> dict[str, Any]:
    """Verify, back up, and restore a stopped server without replacing its root.

    ``backup_dir`` is an exact, previously nonexistent destination. It receives a
    complete checkpoint with this tool's marker and can itself be restored. All
    hashes/paths and the current data tree are validated before data changes. If
    replacement fails, the complete pre-restore backup is used for rollback.
    A rollback failure is explicit and leaves that backup available to an operator.
    """
    checkpoint, data, backup = _path(checkpoint_dir), _path(data_dir), _path(backup_dir)
    _distinct(checkpoint, data, backup)
    journal = _journal_path(data)
    if journal.exists() or journal.is_symlink():
        raise RuntimeError("An interrupted restore requires recovery before another restore")
    if read_pending_restore_receipt(data) is not None:
        raise RuntimeError("The previous world restore receipt must be committed to the experiment ledger and acknowledged before another restore")
    if backup.exists():
        raise FileExistsError("Backup destination must not already exist")
    manifest = _validated_checkpoint(checkpoint)
    if expected_run_id is not None:
        metadata = manifest["metadata"]
        if not isinstance(expected_run_id, str) or not expected_run_id or metadata.get("run_id") != expected_run_id:
            raise ValueError("Checkpoint does not belong to the expected experiment run")
        if any(type(metadata.get(key)) is not int or metadata[key] < 0 for key in ("world_epoch", "restore_point_event_seq")):
            raise ValueError("Checkpoint lacks a valid world epoch and restore-point evidence cutoff")
    _scan(data)
    backup_manifest = _build(data, backup, {
        "kind": "pre_restore_backup", "restored_checkpoint_id": manifest["checkpoint_id"],
        "restored_checkpoint_metadata": manifest["metadata"]}, "backup-" + uuid.uuid4().hex, min_free_disk_bytes)
    _require_space(data, max(0, sum(entry["size"] for entry in manifest["files"].values()) - sum(entry["size"] for entry in backup_manifest["files"].values())), min_free_disk_bytes)
    restore_id = uuid.uuid4().hex
    _write_journal(journal, {"marker": MARKER, "schema_version": 1, "data_dir": str(data),
                             "checkpoint_dir": str(checkpoint), "backup_dir": str(backup), "restore_id": restore_id})
    try:
        _restore_contents(checkpoint, data, manifest)
    except BaseException as original:
        try:
            _restore_contents(backup, data, backup_manifest)
        except BaseException as rollback:
            raise RuntimeError(f"Restore and automatic rollback failed; recover manually from {backup}") from rollback
        _clear_journal(journal)
        raise RuntimeError(f"Restore failed; original data was restored from {backup}") from original
    receipt = {"marker": MARKER, "schema_version": 1, "restore_id": restore_id,
               "checkpoint_id": manifest["checkpoint_id"], "checkpoint_dir": str(checkpoint),
               "data_dir": str(data), "backup_dir": str(backup), "metadata": manifest["metadata"],
               "restored_files": len(manifest["files"]), "completed_at": datetime.now(timezone.utc).isoformat()}
    # This completion record bridges the filesystem/SQLite transaction gap.
    # It must become durable BEFORE the recovery journal can be removed.
    _write_journal(_receipt_path(data), receipt)
    _clear_journal(journal)
    return {"restore_id": restore_id, "checkpoint_id": manifest["checkpoint_id"], "checkpoint_dir": str(checkpoint),
            "data_dir": str(data), "backup_dir": str(backup), "metadata": manifest["metadata"],
            "restored_files": len(manifest["files"]), "files": manifest["files"]}


def recover_interrupted_restore(data_dir: str | Path) -> dict[str, Any] | None:
    """Recover the pre-restore world after process/power loss, with server stopped.

    The journal lives beside the Docker bind root so a partial data replacement
    cannot erase it. Conservative recovery aborts an uncommitted restore even
    if all target files happened to finish copying before the process died.
    """
    data = _path(data_dir)
    journal = _journal_path(data)
    if not journal.exists() and not journal.is_symlink():
        return None
    if journal.is_symlink() or not stat.S_ISREG(journal.lstat().st_mode):
        raise ValueError("Restore recovery journal must be a regular file")
    document = json.loads(journal.read_text())
    if document.get("marker") != MARKER or document.get("schema_version") != 1 or document.get("data_dir") != str(data):
        raise ValueError("Invalid restore recovery journal")
    backup = _path(document["backup_dir"])
    checkpoint = _path(document["checkpoint_dir"])
    _distinct(data, backup, checkpoint)
    manifest = _validated_checkpoint(backup)
    if manifest["metadata"].get("kind") != "pre_restore_backup":
        raise ValueError("Recovery backup is not a pre-restore checkpoint")
    receipt = _receipt_path(data)
    if receipt.exists() or receipt.is_symlink():
        # A journal plus a receipt means a filesystem restore had not yet
        # committed. Validate identity before deleting any completion record.
        pending = _read_receipt(data)
        if pending.get("restore_id") != document.get("restore_id") or pending.get("backup_dir") != str(backup):
            raise ValueError("Restore journal and completion receipt identities differ")
    _restore_contents(backup, data, manifest)
    if receipt.exists() or receipt.is_symlink():
        _clear_journal(receipt)
    _clear_journal(journal)
    return {"data_dir": str(data), "backup_dir": str(backup), "metadata": manifest["metadata"],
            "recovered_files": len(manifest["files"])}


recover_pending_restore = recover_interrupted_restore


def _read_receipt(data: Path) -> dict[str, Any] | None:
    path = _receipt_path(data)
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Restore completion receipt must be a regular file")
    document = json.loads(path.read_text())
    if not isinstance(document, dict) or document.get("marker") != MARKER or document.get("schema_version") != 1 or document.get("data_dir") != str(data):
        raise ValueError("Invalid restore completion receipt")
    identity = document.get("restore_id")
    if not isinstance(identity, str) or len(identity) != 32 or any(character not in "0123456789abcdef" for character in identity):
        raise ValueError("Invalid restore receipt identity")
    if not isinstance(document.get("metadata"), dict) or not isinstance(document.get("checkpoint_id"), str):
        raise ValueError("Invalid restore receipt checkpoint metadata")
    _distinct(data, _path(document["checkpoint_dir"]), _path(document["backup_dir"]))
    return document


def read_pending_restore_receipt(data_dir: str | Path) -> dict[str, Any] | None:
    """Read a committed filesystem restore awaiting ledger acknowledgement.

    The caller must first recover a pending journal. It must check the receipt's
    run metadata and durably record rollback lineage before acknowledging it.
    """
    data = _path(data_dir)
    if has_interrupted_restore(data):
        raise RuntimeError("Pending restore journal must be recovered before reading a completion receipt")
    return _read_receipt(data)


def acknowledge_restore_receipt(data_dir: str | Path, restore_id: str) -> None:
    data = _path(data_dir)
    receipt = read_pending_restore_receipt(data)
    if receipt is None:
        raise ValueError("No pending restore receipt to acknowledge")
    if receipt["restore_id"] != restore_id:
        raise ValueError("Restore receipt identity mismatch")
    _clear_journal(_receipt_path(data))
