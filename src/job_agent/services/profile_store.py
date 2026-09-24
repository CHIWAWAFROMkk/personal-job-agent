from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from job_agent.models.profile import Profile


class ProfileStoreError(RuntimeError):
    pass


def load_profile(path: Path) -> Profile:
    if (path.parent / "backups" / "profile-switch-pending.json").exists():
        raise ProfileStoreError("上次切换用户尚未恢复，已暂停资料读取。请关闭并重新启动桌面程序以恢复。")
    if not path.is_file():
        raise ProfileStoreError(f"Profile 不存在: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return Profile.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ProfileStoreError(f"Profile 无法读取或校验失败: {exc}") from exc


def save_profile(
    profile: Profile,
    path: Path,
    *,
    overwrite: bool = False,
    create_backup: bool = True,
) -> Path:
    path = path.resolve()
    if (path.parent / "backups" / "profile-switch-pending.json").exists():
        raise ProfileStoreError("上次切换用户尚未恢复，已暂停资料保存。请关闭并重新启动桌面程序以恢复。")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise ProfileStoreError(f"Profile 已存在，拒绝覆盖: {path}")

    if path.exists() and create_backup:
        backup_dir = path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = backup_dir / f"{path.stem}-{stamp}{path.suffix}"
        if backup_path.exists():
            raise ProfileStoreError(f"备份文件已存在，拒绝覆盖: {backup_path}")
        shutil.copy2(path, backup_path)

    profile.updated_at = datetime.now(UTC)
    payload = profile.model_dump_json(indent=2)
    return write_text_atomic(payload + "\n", path)


def write_json_atomic(payload: object, path: Path) -> Path:
    return write_text_atomic(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        path,
    )


def write_text_atomic(content: str, path: Path, *, encoding: str = "utf-8") -> Path:
    """Write *content* to *path* atomically via a temporary file."""
    return _write_atomic(content, path, encoding=encoding)


def write_bytes_atomic(data: bytes, path: Path) -> Path:
    """Write *data* to *path* atomically via a temporary file."""
    return _write_atomic(data, path)


def _write_atomic(
    content: str | bytes, path: Path, *, encoding: str | None = None
) -> Path:
    """Publish one complete write; concurrent writers use independent files.

    This does not serialize read-modify-write operations: the last successful
    replacement wins. The temporary file is closed before replacement on Windows.
    """
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w" if isinstance(content, str) else "wb",
            encoding=encoding,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        for attempt in range(4):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                # Windows can briefly deny replacement during another replacement
                # or a reader's open handle. Keep retries short and bounded.
                if os.name != "nt" or attempt == 3:
                    raise
                time.sleep(0.01 * (2**attempt))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path
