"""Recover an interrupted profile switch before exposing either user's data.

The database's transaction marker is the commit decision. Filesystem recovery is
repeatable and preserves displaced new files; it never restores a stale DB copy.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from job_agent.services.profile_store import write_bytes_atomic


PENDING_NAME = "profile-switch-pending.json"


class ProfileRecoveryError(RuntimeError):
    pass


def finish_record(marker: Path, suffix: str, outcome: str) -> None:
    marker.replace(marker.parent / f"profile-switch-{outcome}-{suffix}.json")


def _safe_path(path: Path, boundary: Path) -> Path:
    if not path.is_relative_to(boundary):
        raise ProfileRecoveryError("恢复路径超出资料目录。")
    current = path
    while current != boundary.parent:
        if current.is_symlink() or current.is_junction():
            raise ProfileRecoveryError("恢复路径包含链接，已停止恢复以保护资料。")
        current = current.parent
    return path


def _names(value: object) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(name, str) or not name or Path(name).name != name
        or name in {".", ".."} or ":" in name or "/" in name or "\\" in name
        for name in value
    ) or len({name.casefold() for name in value}) != len(value):
        raise ProfileRecoveryError("切换记录中的文件名无效。")
    return value


def validate_record(marker: Path, *, profile_path: Path, private_dir: Path,
                    output_dir: Path, database_path: Path) -> dict:
    boundary = private_dir.parent
    for path in (marker, profile_path, output_dir, database_path):
        _safe_path(path, boundary)
    record = json.loads(marker.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ProfileRecoveryError("切换恢复记录必须为对象。")
    suffix = record.get("suffix", "")
    if record.get("version") != 1 or not isinstance(suffix, str) or not re.fullmatch(
        r"\d{8}T\d{12}Z-[0-9a-f]{8}", suffix
    ):
        raise ProfileRecoveryError("切换恢复记录格式无效。")
    if record.get("profile_path") != str(profile_path) or record.get("database_path") != str(database_path):
        raise ProfileRecoveryError("切换恢复记录与当前资料位置不一致。")
    if output_dir != boundary / "output":
        raise ProfileRecoveryError("恢复输出路径不是专用 data/output。")
    old_names = _names(record.get("old_private_names"))
    new_names = _names(record.get("new_private_names"))
    reserved = {"backups", "app-settings.json", "api-usage.json", "agent-token.json",
                profile_path.name, database_path.name, database_path.name + "-wal",
                database_path.name + "-shm", database_path.name + "-journal"}
    if {name.casefold() for name in reserved}.intersection(name.casefold() for name in old_names + new_names):
        raise ProfileRecoveryError("切换恢复记录包含保留文件。")
    for name in old_names + new_names:
        _safe_path(private_dir / name, boundary)
    archive = marker.parent / f"profile-switch-private-{suffix}"
    _safe_path(archive, boundary)
    if archive.exists():
        if not archive.is_dir() or set(p.name for p in archive.iterdir()) - set(old_names):
            raise ProfileRecoveryError("资料归档与切换恢复记录不一致。")
        for item in archive.iterdir():
            _safe_path(item, boundary)
    for path in (output_dir.with_name(f"output-profile-switch-{suffix}"),
                 marker.parent / f"recovered-profile-switch-{suffix}"):
        _safe_path(path, boundary)
    if not isinstance(record.get("old_output_exists"), bool):
        raise ProfileRecoveryError("切换恢复记录缺少输出目录状态。")
    digest = record.get("old_profile_sha256")
    if "old_profile_sha256" not in record or (digest is not None and (
        not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
    )):
        raise ProfileRecoveryError("切换恢复记录缺少有效的原资料校验值。")
    if digest is not None:
        backup = marker.parent / f"{profile_path.stem}-switch-{suffix}{profile_path.suffix}"
        _safe_path(backup, boundary)
        if not backup.is_file() or hashlib.sha256(backup.read_bytes()).hexdigest() != digest:
            raise ProfileRecoveryError("原资料备份缺失或校验失败，未覆盖当前资料。")
    return record


def restore_files(record: dict, *, marker: Path, profile_path: Path,
                  private_dir: Path, output_dir: Path) -> None:
    suffix = record["suffix"]
    archive = marker.parent / f"profile-switch-private-{suffix}"
    recovery = marker.parent / f"recovered-profile-switch-{suffix}"
    recovery.mkdir(exist_ok=True)

    def preserve(path: Path, name: str) -> None:
        destination = recovery / name
        _safe_path(destination, private_dir.parent)
        if destination.exists():
            # Startup may recreate empty folders before a repeated recovery.
            # Keep every displaced copy instead of replacing an earlier one.
            destination = recovery / f"{name}-{uuid.uuid4().hex}"
        path.replace(destination)

    for name in record["new_private_names"]:
        current = private_dir / name
        if current.exists() and (name not in record["old_private_names"] or (archive / name).exists()):
            preserve(current, name)
    for name in record["old_private_names"]:
        saved = archive / name
        if saved.exists():
            if (private_dir / name).exists():
                # Desktop startup recreates browser directories. Preserve any
                # such fresh content before returning the old user's archive.
                preserve(private_dir / name, "current-" + name)
            saved.replace(private_dir / name)
        elif not (private_dir / name).exists():
            raise ProfileRecoveryError(f"原资料文件及归档均缺失: {name}")

    old_digest = record["old_profile_sha256"]
    current_digest = hashlib.sha256(profile_path.read_bytes()).hexdigest() if profile_path.is_file() else None
    if old_digest != current_digest:
        if profile_path.exists():
            preserve(profile_path, "new-profile.json")
        if old_digest is not None:
            backup = marker.parent / f"{profile_path.stem}-switch-{suffix}{profile_path.suffix}"
            write_bytes_atomic(backup.read_bytes(), profile_path)
    output_archive = output_dir.with_name(f"output-profile-switch-{suffix}")
    if output_archive.exists():
        if output_dir.exists():
            preserve(output_dir, "new-output")
        output_archive.replace(output_dir)
    elif not record["old_output_exists"] and output_dir.exists():
        preserve(output_dir, "new-output")
    elif record["old_output_exists"] and not output_dir.is_dir():
        raise ProfileRecoveryError("原输出目录与归档均缺失。")


def recover_interrupted_profile_switch(*, profile_path: Path, private_dir: Path,
                                      output_dir: Path, repository) -> str | None:
    """Called before startup; return rolled_back/committed, or fail closed."""
    private_dir = private_dir.absolute()
    profile_path = profile_path.absolute()
    output_dir = output_dir.absolute()
    marker = private_dir / "backups" / PENDING_NAME
    if not marker.exists():
        return None
    try:
        database_path = repository.path.absolute()
        record = validate_record(marker, profile_path=profile_path, private_dir=private_dir,
                                 output_dir=output_dir, database_path=database_path)
        # mode=rw forbids silently creating an empty replacement for a missing DB.
        with closing(sqlite3.connect(database_path.as_uri() + "?mode=rw", uri=True, timeout=1)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not marker.exists():
                return None
            row = connection.execute("SELECT value FROM schema_meta WHERE key='profile_switch_id'").fetchone()
            outcome = "committed" if row and row[0] == record["suffix"] else "rolled_back"
            if outcome == "rolled_back":
                restore_files(record, marker=marker, profile_path=profile_path,
                              private_dir=private_dir, output_dir=output_dir)
            finish_record(marker, record["suffix"], outcome)
        return outcome
    except (OSError, ValueError, TypeError, sqlite3.Error, ProfileRecoveryError) as exc:
        raise ProfileRecoveryError(
            f"上次切换用户未完成，已停止启动以保护资料。原文件及备份均予保留；"
            f"请先关闭其他本程序窗口后重试。若仍失败，请保留此目录并联系维护者: {marker.parent}。原因: {exc}"
        ) from exc
