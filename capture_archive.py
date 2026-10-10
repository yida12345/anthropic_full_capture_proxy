"""已收尾 capture 的定时无损归档及离线恢复。只使用 Python 标准库。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone
from contextlib import closing
from itertools import chain
from pathlib import Path, PurePosixPath

from capture_core import utc_timestamp

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
CLEANUP_DIRECTORY = ".archive-cleanup"


def encode_manifest(manifest: dict) -> bytes:
    return json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()


def sync_directory(path: Path) -> None:
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def files_by_capture(manifest: dict) -> dict:
    captures = {}
    for name, entry in manifest["files"].items():
        captures.setdefault(member_name(name), {})[name] = entry
    return captures


def record_failure(result: dict, stage: str, error: Exception, capture_id: str) -> None:
    result["failures"] += 1
    result["last_failure"] = {"stage": stage, "error_type": type(error).__name__,
        "capture_id": capture_id if re.fullmatch(r"[A-Za-z0-9_-]+", capture_id) else None,
        "at": utc_timestamp()}


def digest_file(path: Path) -> str:
    with path.open("rb") as handle:
        return digest_stream(handle)


def digest_stream(handle) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def member_name(name: str) -> str:
    parts = PurePosixPath(name).parts
    if (len(parts) != 3 or parts[0] != "completed"
            or any(p in {".", ".."} for p in parts)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[1])
            or "\\" in name or str(PurePosixPath(name)) != name):
        raise ValueError("归档成员路径无效")
    return parts[1]


def snapshot(directory: Path, *, allow_empty=False) -> dict:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("capture 必须是普通目录")
    files = {}
    for path in sorted(directory.iterdir()):
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("capture 中存在非普通文件")
        name = f"completed/{directory.name}/{path.name}"
        member_name(name)
        before = path.stat()
        digest = digest_file(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError("capture 在读取期间变化")
        files[name] = {"size": after.st_size, "sha256": digest}
    if not files and not allow_empty:
        raise ValueError("capture 为空")
    return files


def verify_archive(path: Path) -> dict:
    """逐文件读回并校验，不信任 tar 路径、链接或清单内容。"""
    with tarfile.open(path, "r:gz") as archive:
        first = archive.next()
        if first is None or first.name != "manifest.json" or not first.isfile() or first.size > MAX_MANIFEST_BYTES:
            raise ValueError("归档缺少有效清单")
        manifest = json.load(archive.extractfile(first))
        if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), dict):
            raise ValueError("归档清单格式无效")
        expected = manifest["files"]
        if not expected:
            raise ValueError("归档清单为空")
        for name, entry in expected.items():
            member_name(name)
            if (not isinstance(entry, dict) or type(entry.get("size")) is not int
                    or entry["size"] < 0
                    or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", "")))):
                raise ValueError("文件哈希清单无效")
        seen = set()
        for member in archive:
            # tarfile 的迭代器会再次返回已读取的首成员。
            if member is first:
                continue
            member_name(member.name)
            if not member.isfile() or member.name in seen or member.name not in expected:
                raise ValueError("归档包含额外成员、重复成员或链接")
            entry = expected[member.name]
            if member.size != entry["size"]:
                raise ValueError("归档文件大小不匹配")
            with archive.extractfile(member) as handle:
                digest = digest_stream(handle)
            if digest != entry["sha256"]:
                raise ValueError("归档文件哈希不匹配")
            seen.add(member.name)
        if seen != set(expected):
            raise ValueError("归档缺失文件")
        return manifest


def write_archive(root: Path, destination: Path, captures: list[Path], *, manifest=None) -> dict:
    if manifest is None:
        files = {}
        for directory in captures:
            files.update(snapshot(directory))
        manifest = {"schema_version": 1, "created_at": utc_timestamp(),
                    "source_root": str(root.resolve()), "files": files}
    payload = encode_manifest(manifest)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ValueError("归档清单超过大小上限")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with tarfile.open(temporary, "w:gz", compresslevel=6) as archive:
            info = tarfile.TarInfo("manifest.json")
            info.size = len(payload)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(payload))
            for name in manifest["files"]:
                archive.add(root / name, arcname=name, recursive=False)
        if verify_archive(temporary) != manifest:
            raise ValueError("归档清单读回不一致")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        # 目录项也落盘，然后才能写索引和清理源文件。
        sync_directory(destination.parent)
        return manifest
    finally:
        temporary.unlink(missing_ok=True)


def remove_verified(directory: Path, manifest: dict, *, allow_partial=False) -> None:
    expected = manifest["files"]
    actual = snapshot(directory, allow_empty=allow_partial)
    matches = (all(expected.get(name) == entry for name, entry in actual.items())
               if allow_partial else actual == expected)
    if not expected or not matches:
        raise ValueError("源 capture 与归档不一致，保留源文件")
    # 先原子移出 completed。中断后，残余文件不依赖已删除的 state.json，
    # 下一轮根据持久化索引和仍可完整校验的归档，校验剩余文件后继续清理。
    if directory.parent.name != CLEANUP_DIRECTORY:
        cleanup = directory.parent.parent / CLEANUP_DIRECTORY
        cleanup.mkdir(exist_ok=True)
        if cleanup.is_symlink():
            raise ValueError("清理目录不能是符号链接")
        sync_directory(cleanup.parent)
        destination = cleanup / directory.name
        if destination.exists():
            raise FileExistsError("清理区已有同名 capture")
        previous = directory.parent
        os.rename(directory, destination)
        directory = destination
        sync_directory(previous)
        sync_directory(cleanup)
    shutil.rmtree(directory)
    sync_directory(directory.parent)


def archive_completed(root: Path, archive_dir: Path, *, min_age_seconds: float,
                      batch_size: int, delete_source: bool, excluded_ids=()) -> dict:
    """总条数受批量限制，清单按字节分包；单条故障不会中断其它 capture。"""
    result = {"archived_captures": 0, "removed_captures": 0, "archive_bytes": 0,
              "failures": 0, "last_failure": None}
    completed = root / "completed"
    cleanup = root / CLEANUP_DIRECTORY
    if not completed.exists() and not cleanup.exists():
        return result
    archive_dir.mkdir(parents=True, exist_ok=True)
    source = str(root.resolve())
    excluded = set(excluded_ids)
    with closing(sqlite3.connect(archive_dir / "index.sqlite3", timeout=0)) as index, index:
        index.execute("CREATE TABLE IF NOT EXISTS archives (archive TEXT PRIMARY KEY, manifest TEXT)")
        index.execute("CREATE TABLE IF NOT EXISTS captures (source TEXT, capture TEXT, archive TEXT, PRIMARY KEY(source, capture))")
        index.execute("BEGIN IMMEDIATE")
        # 已归档记录只查询路径；仅在清理或修复旧包时加载一次整包清单。
        indexed, indexed_manifests, verified = {}, {}, {}

        def indexed_files(relative):
            if relative not in indexed:
                row = index.execute("SELECT manifest FROM archives WHERE archive=?", (relative,)).fetchone()
                if row is None:
                    raise ValueError("归档索引缺少清单")
                indexed_manifests[relative] = json.loads(row[0])
                indexed[relative] = files_by_capture(indexed_manifests[relative])
            return indexed[relative]

        def verified_files(relative):
            if relative not in verified:
                try:
                    manifest = verify_archive(archive_dir / relative)
                    indexed_files(relative)
                    if manifest != indexed_manifests[relative]:
                        raise ValueError("归档与持久化索引不一致")
                    verified[relative] = files_by_capture(manifest)
                except Exception as exc:
                    # 失败也缓存，避免同包的每条 capture 重复解压坏包。
                    verified[relative] = exc
            value = verified[relative]
            if isinstance(value, Exception):
                raise value
            return value

        candidates = []
        cleanup_items = ()
        if delete_source and cleanup.exists():
            if cleanup.is_symlink():
                raise ValueError("清理目录不能是符号链接")
            cleanup_items = cleanup.iterdir()
        directories = chain(cleanup_items, completed.iterdir() if completed.exists() else ())
        template = {"schema_version": 1, "created_at": utc_timestamp(),
                    "source_root": source, "files": {}}
        base_size = len(encode_manifest(template))
        for directory in directories:
            if directory.name in excluded or directory.is_symlink() or not directory.is_dir():
                continue
            phase = "inspect"
            try:
                old = index.execute("SELECT archive FROM captures WHERE source=? AND capture=?",
                                    (source, directory.name)).fetchone()
                if old:
                    old_path = archive_dir / old[0]
                    if not delete_source and old_path.is_file():
                        continue
                    expected = indexed_files(old[0]).get(directory.name)
                    if not expected:
                        raise ValueError("索引缺少 capture 文件")
                    phase = "verify_existing"
                    try:
                        archived = verified_files(old[0])[directory.name]
                    except Exception as exc:
                        record_failure(result, phase, exc, directory.name)
                        # 完整原件与旧清单一致时可以重新归档；残缺原件必须保留。
                        if snapshot(directory) != expected:
                            raise ValueError("旧包不可用且原件不完整，保留残余文件")
                    else:
                        if delete_source:
                            phase = "cleanup"
                            remove_verified(directory, {"files": archived}, allow_partial=True)
                            result["removed_captures"] += 1
                            if len(candidates) + result["removed_captures"] >= batch_size:
                                break
                        continue
                elif directory.parent == cleanup:
                    raise ValueError("清理残余没有持久化归档索引")
                phase = "inspect"
                state = json.loads((directory / "state.json").read_text())
                if state.get("state") not in {"complete", "partial"}:
                    continue
                ended = datetime.fromisoformat(state["updated_at"].replace("Z", "+00:00")).timestamp()
                if time.time() - ended < min_age_seconds:
                    continue
                phase = "snapshot"
                files = snapshot(directory)
                # 每条只序列化自己的成员；合并时只有逗号开销，无平方级清单编码。
                cost = len(encode_manifest(files)) - 2
                if base_size + cost > MAX_MANIFEST_BYTES:
                    raise ValueError("单条 capture 清单超过大小上限")
                candidates.append((directory, files, cost))
                if len(candidates) + result["removed_captures"] >= batch_size:
                    break
            except Exception as exc:
                record_failure(result, phase, exc, directory.name)

        if not candidates:
            return result
        folder = archive_dir / datetime.now(timezone.utc).strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        sync_directory(archive_dir)

        def publish(batch):
            manifest = {**template, "files": {}}
            for _, files, _ in batch:
                manifest["files"].update(files)
            destination = folder / f"captures-{uuid.uuid4().hex}.tar.gz"
            try:
                write_archive(root, destination, [p for p, _, _ in batch], manifest=manifest)
            except Exception as exc:
                # 读取期间单条变化/读盘失败也要隔离；缩小批次定位故障。
                if len(batch) > 1:
                    middle = len(batch) // 2
                    publish(batch[:middle])
                    publish(batch[middle:])
                else:
                    record_failure(result, "write", exc, batch[0][0].name)
                return
            relative = str(destination.relative_to(archive_dir))
            try:
                index.execute("INSERT INTO archives VALUES (?, ?)", (relative, encode_manifest(manifest).decode()))
                index.executemany("INSERT OR REPLACE INTO captures VALUES (?, ?, ?)",
                                  [(source, p.name, relative) for p, _, _ in batch])
                index.commit()
            except Exception as exc:
                index.rollback()
                for directory, _, _ in batch:
                    record_failure(result, "index", exc, directory.name)
                index.execute("BEGIN IMMEDIATE")
                return
            # 重新持锁后才能清理。包和索引均已持久化，发布后失败的重复包可去重恢复。
            index.execute("BEGIN IMMEDIATE")
            result["archived_captures"] += len(batch)
            result["archive_bytes"] += destination.stat().st_size
            if delete_source:
                for directory, files, _ in batch:
                    try:
                        remove_verified(directory, {"files": files})
                        result["removed_captures"] += 1
                    except Exception as exc:
                        record_failure(result, "cleanup", exc, directory.name)

        batch, payload_size = [], base_size
        for item in candidates:
            cost = item[2] + (1 if batch else 0)
            if payload_size + cost > MAX_MANIFEST_BYTES:
                publish(batch)
                batch, payload_size, cost = [], base_size, item[2]
            batch.append(item)
            payload_size += cost
        if batch:
            publish(batch)
    return result


class ArchiveManager:
    def __init__(self, root, archive_dir, *, interval_seconds, min_age_seconds,
                 batch_size, delete_source, active_ids):
        self.root = root
        self.archive_dir = archive_dir
        self.interval_seconds = interval_seconds
        self.min_age_seconds = min_age_seconds
        self.batch_size = batch_size
        self.delete_source = delete_source
        self.active_ids = active_ids
        self.stop = asyncio.Event()
        self.task = None
        self.running = False
        self.runs = self.failures = 0
        self.totals = {"archived_captures": 0, "removed_captures": 0, "archive_bytes": 0}
        self.last_run = self.last_failure = None

    def start(self):
        if self.archive_dir is not None:
            self.task = asyncio.create_task(self.run())

    def failure(self, stage, error):
        self.failures += 1
        self.last_failure = {"stage": stage, "error_type": type(error).__name__, "at": utc_timestamp()}

    async def run(self):
        while not self.stop.is_set():
            self.running = True
            try:
                result = await asyncio.to_thread(archive_completed, self.root, self.archive_dir,
                    min_age_seconds=self.min_age_seconds, batch_size=self.batch_size,
                    delete_source=self.delete_source, excluded_ids=self.active_ids())
                for key in self.totals:
                    self.totals[key] += result[key]
                self.failures += result.get("failures", 0)
                if result.get("last_failure"):
                    self.last_failure = result["last_failure"]
            except Exception as exc:
                self.failure("archive", exc)
            finally:
                self.runs += 1
                self.last_run = utc_timestamp()
                self.running = False
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass

    def metrics(self):
        return {"enabled": self.archive_dir is not None,
                "status": "degraded" if self.failures else "ok", "running": self.running,
                "directory": str(self.archive_dir) if self.archive_dir is not None else None,
                "interval_seconds": self.interval_seconds, "min_age_seconds": self.min_age_seconds,
                "batch_size": self.batch_size, "delete_source": self.delete_source,
                "runs": self.runs, **self.totals, "failures": self.failures,
                "last_run": self.last_run, "last_failure": self.last_failure}

    async def close(self, timeout=5):
        self.stop.set()
        if self.task is not None:
            _, pending = await asyncio.wait([self.task], timeout=timeout)
            if pending:
                # 不取消线程中的压缩/校验，避免误把尚未结束的操作当成完成。
                self.failure("shutdown_timeout", TimeoutError())


def restore_archives(archives: list[Path], output: Path) -> int:
    """校验后恢复 completed/；拒绝覆盖已有 capture 或提取不安全 tar 成员。"""
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise FileExistsError("恢复输出目录须不存在或为空")
    manifests = [verify_archive(path) for path in archives]
    captures = {}
    for manifest in manifests:
        for identifier, files in files_by_capture(manifest).items():
            if identifier in captures and captures[identifier] != files:
                raise ValueError("重复 capture 的文件清单冲突")
            captures[identifier] = files
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".capture-restore-", dir=output.parent) as temporary:
        stage = Path(temporary)
        for path, manifest in zip(archives, manifests):
            with tarfile.open(path, "r:gz") as archive:
                first = archive.next()
                if (first is None or first.name != "manifest.json" or not first.isfile()
                        or first.size > MAX_MANIFEST_BYTES
                        or json.load(archive.extractfile(first)) != manifest):
                    raise ValueError("恢复时归档清单发生变化")
                seen = set()
                for member in archive:
                    if member is first:
                        continue
                    member_name(member.name)
                    if not member.isfile() or member.name in seen or member.name not in manifest["files"]:
                        raise ValueError("恢复时归档成员发生变化")
                    seen.add(member.name)
                    destination = stage / member.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    entry = manifest["files"][member.name]
                    if member.size != entry["size"]:
                        raise ValueError("恢复时文件大小不匹配")
                    with archive.extractfile(member) as handle:
                        if destination.exists():
                            # 完整清单一致的重复仍读回校验，不能静默忽略坏包。
                            digest = digest_stream(handle)
                        else:
                            with destination.open("xb") as target:
                                shutil.copyfileobj(handle, target)
                            digest = digest_file(destination)
                    if destination.stat().st_size != entry["size"] or digest != entry["sha256"]:
                        raise ValueError("恢复时哈希不匹配")
                if seen != set(manifest["files"]):
                    raise ValueError("恢复时归档缺失文件")
        (output / "completed").mkdir(parents=True, exist_ok=True)
        for value in captures:
            destination = output / "completed" / value
            if destination.exists():
                raise FileExistsError("恢复目录出现同名 capture")
            os.rename(stage / "completed" / value, destination)
    return len(captures)


def main():
    parser = argparse.ArgumentParser(description="校验并恢复自动归档的原始 capture")
    parser.add_argument("--archives", type=Path, nargs="+", required=True)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="恢复后的 raw 根目录；须不存在或为空，不覆盖已有 capture")
    args = parser.parse_args()
    print(json.dumps({"restored_captures": restore_archives(args.archives, args.output_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
