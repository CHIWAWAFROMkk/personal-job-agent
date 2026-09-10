from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from job_agent.models.profile import Profile


class ProfileStoreError(RuntimeError):
    pass


def load_profile(path: Path) -> Profile:
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
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def write_json_atomic(payload: object, path: Path) -> Path:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path
