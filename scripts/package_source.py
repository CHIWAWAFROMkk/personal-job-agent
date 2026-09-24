"""Build a privacy-audited, versioned source ZIP without runtime data."""
from __future__ import annotations
import argparse
from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import zipfile

if __package__:
    from .privacy_audit import SKIP_DIRS
else:
    from privacy_audit import SKIP_DIRS

SOURCE_DIRS = {"src", "scripts", "tests", "docs", "evals", "packaging", "references", ".github"}
ROOT_FILES = {
    "pyproject.toml", "README.md", "PRODUCT.md", "DESIGN.md", "CHANGELOG.md",
    "LICENSE", "PRIVACY.md", "SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md",
    ".env.example", ".gitignore", ".gitattributes",
    "安装并启动求职Agent.cmd", "启动求职Agent.cmd",
}
SKIP_PARTS = {part.casefold() for part in SKIP_DIRS} | {".vscode", "node_modules", "browser-profile", "browser-data"}

def source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    def add(path: Path) -> None:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError(f"Source package cannot follow a link: {path.relative_to(root)}")
        if path.is_file() and path.suffix not in {".pyc", ".pyo"}:
            files.append(path)
    for name in ROOT_FILES | {"data/private/.gitkeep", "data/inbox/.gitkeep", "data/output/.gitkeep"}:
        add(root / name)
    for name in SOURCE_DIRS:
        top = root / name
        add(top)
        for directory, dirs, names in os.walk(top, followlinks=False):
            dirs[:] = [d for d in dirs if d.casefold() not in SKIP_PARTS and not d.casefold().endswith(".egg-info")]
            for d in dirs:
                add(Path(directory) / d)
            for filename in names:
                add(Path(directory) / filename)
    return sorted(files)

def build(root: Path, output: Path | None = None) -> Path:
    root = root.resolve()
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if not isinstance(version, str) or not version or any(c not in "0123456789abcdefghijklmnopqrstuvwxyz.-+" for c in version):
        raise ValueError("Invalid project version")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    target = (output or root / "dist" / "review" / f"PersonalJobAgent-{version}-source-review-{stamp}.zip").resolve()
    if target.exists():
        raise FileExistsError("Refusing to replace an existing release archive")
    files = source_files(root)
    # Audit exactly what will be delivered, including untracked source files.
    stage = root / "work" / "source-package" / stamp
    stage.mkdir(parents=True, exist_ok=False)
    for path in files:
        dest = stage / path.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(path.read_bytes())
    audit = root / "scripts" / "privacy_audit.py"
    subprocess.run([sys.executable, str(audit), str(stage)], check=True, timeout=60)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(stage).as_posix())
    subprocess.run([sys.executable, str(audit), str(target)], check=True, timeout=60)
    with target.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    target.with_suffix(target.suffix + ".sha256.txt").write_text(f"{digest}  {target.name}\n", encoding="ascii")
    print(f"Source archive: {target}\nSHA256: {digest}\nFiles: {len(files)}")
    return target

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    build(args.root, args.output)

if __name__ == "__main__":
    main()
