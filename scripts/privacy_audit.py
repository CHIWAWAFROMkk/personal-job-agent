"""Fail closed when a source tree or release contains likely private material.

The audit reports categories and file paths only. It never prints matched secrets or
profile values. An optional private profile can be supplied for a one-way comparison.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path


SKIP_DIRS = {
    ".git",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".workbuddy",
    "__pycache__",
    "build",
    "dist",
    "work",
}
FORBIDDEN_EXTENSIONS = {
    ".7z",
    ".bak",
    ".csv",
    ".db",
    ".doc",
    ".docx",
    ".env",
    ".gif",
    ".jpeg",
    ".jpg",
    ".key",
    ".log",
    ".p12",
    ".pdf",
    ".pem",
    ".pfx",
    ".png",
    ".sqlite",
    ".sqlite3",
    ".webp",
    ".xls",
    ".xlsx",
    ".zip",
}
ALLOWED_EMAIL_DOMAINS = {"example.com", "users.noreply.github.com"}
TEXT_EXTENSIONS = {
    "",
    ".cmd",
    ".css",
    ".example",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "Windows user-profile path",
        re.compile(r"(?i)\b[A-Z]:[\\/](?:Users|用户)[\\/][^\\/\s\"']+"),
    ),
    ("WeChat identifier", re.compile(r"(?i)\bwxid_[a-z0-9_-]{5,}\b")),
    ("OpenAI-style secret", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
    ("private key material", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
SENSITIVE_PROFILE_KEYS = {
    "address",
    "city",
    "display_name",
    "email",
    "institution",
    "legal_name",
    "organization",
    "original_path",
    "phone",
    "statement",
    "summary",
}


def _ignored(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return any(part in SKIP_DIRS or part.endswith(".egg-info") for part in relative.parts)


def _git_files(root: Path) -> list[Path] | None:
    if not (root / ".git").exists():
        return None
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        return None
    return [root / item.decode("utf-8") for item in completed.stdout.split(b"\0") if item]


def iter_files(root: Path) -> Iterable[Path]:
    tracked = _git_files(root)
    candidates = tracked if tracked is not None else root.rglob("*")
    for path in candidates:
        if path.is_file() and not _ignored(path, root):
            yield path


def _is_release_dependency(relative: Path) -> bool:
    return "_internal" in {part.casefold() for part in relative.parts}


def _is_forbidden_user_file(relative: Path, *, release_mode: bool) -> bool:
    normalized = relative.as_posix().lower()
    if normalized in {".env", "profile.json", "app-settings.json"}:
        return True
    if normalized.startswith(("data/private/", "data/inbox/", "data/output/")):
        return relative.name != ".gitkeep"
    if release_mode and _is_release_dependency(relative):
        return False
    return relative.suffix.lower() in FORBIDDEN_EXTENSIONS


def _decode_views(payload: bytes) -> list[str]:
    return [
        payload.decode("utf-8", errors="ignore"),
        payload.decode("utf-16-le", errors="ignore"),
    ]


def _profile_tokens(node: object, path: tuple[str, ...] = ()) -> list[tuple[str, bytes]]:
    tokens: list[tuple[str, bytes]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            tokens.extend(_profile_tokens(value, (*path, str(key))))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            tokens.extend(_profile_tokens(value, (*path, str(index))))
    elif isinstance(node, str) and path:
        leaf = path[-1]
        value = node.strip()
        if leaf in SENSITIVE_PROFILE_KEYS and len(value) >= 4:
            tokens.append(("private profile fingerprint", value.casefold().encode("utf-8")))
    return tokens


def _local_path_tokens(root: Path) -> list[tuple[str, bytes]]:
    paths = {Path.home().resolve()}
    resolved = root.resolve()
    parts = resolved.parts
    for index, part in enumerate(parts[:-1]):
        if part.casefold() in {"users", "用户"} and index + 1 < len(parts):
            paths.add(Path(*parts[: index + 2]))
    tokens: list[tuple[str, bytes]] = []
    for path in paths:
        for value in {str(path), str(path).replace("\\", "/")}:
            tokens.append(("local build-path fingerprint", value.casefold().encode("utf-8")))
    return tokens


def audit(
    root: Path,
    private_profile: Path | None,
    *,
    release_mode: bool = False,
    check_local_paths: bool = False,
) -> list[tuple[str, str]]:
    findings: set[tuple[str, str]] = set()
    profile_tokens: list[tuple[str, bytes]] = []
    if private_profile is not None:
        profile = json.loads(private_profile.read_text(encoding="utf-8"))
        profile_tokens = _profile_tokens(profile)
    if check_local_paths:
        profile_tokens.extend(_local_path_tokens(root))

    for path in iter_files(root):
        relative = path.relative_to(root)
        display_path = relative.as_posix()
        if _is_forbidden_user_file(relative, release_mode=release_mode):
            findings.add(("private or release-only file type", display_path))
            continue

        try:
            payload = path.read_bytes()
        except OSError:
            findings.add(("unreadable file", display_path))
            continue

        folded_utf8 = payload.lower()
        for category, token in profile_tokens:
            if token in folded_utf8 or token.decode("utf-8").encode("utf-16-le") in payload.lower():
                findings.add((category, display_path))

        for text in _decode_views(payload):
            for category, pattern in TEXT_PATTERNS:
                if (
                    category == "Windows user-profile path"
                    and release_mode
                    and _is_release_dependency(relative)
                ):
                    continue
                if pattern.search(text):
                    findings.add((category, display_path))
            for match in EMAIL_PATTERN.finditer(text) if relative.suffix.lower() in TEXT_EXTENSIONS else ():
                if (
                    match.group(1).casefold() not in ALLOWED_EMAIL_DOMAINS
                    and not (release_mode and _is_release_dependency(relative))
                ):
                    findings.add(("non-example email address", display_path))

    return sorted(findings, key=lambda item: (item[1].casefold(), item[0]))


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan a source tree or release for private material.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--private-profile", type=Path)
    parser.add_argument(
        "--release",
        action="store_true",
        help="Allow signed third-party resources under the packaged _internal directory.",
    )
    parser.add_argument(
        "--check-local-paths",
        action="store_true",
        help="Compare files with this machine's user-profile roots without printing them.",
    )
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if not root.is_dir():
        parser.error(f"not a directory: {root}")
    if args.private_profile is not None and not args.private_profile.is_file():
        parser.error("private profile file not found")

    findings = audit(
        root,
        args.private_profile,
        release_mode=args.release,
        check_local_paths=args.check_local_paths,
    )
    if findings:
        print(f"Privacy audit failed with {len(findings)} finding(s):", file=sys.stderr)
        for category, path in findings:
            print(f"- {category}: {path}", file=sys.stderr)
        return 1
    print(f"Privacy audit passed: {sum(1 for _ in iter_files(root))} files checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
