from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from job_agent.services.runtime_config import effective_runtime_config


def _looks_like_project_root(path: Path) -> bool:
    return (
        (path / "pyproject.toml").is_file()
        and (path / "src" / "job_agent").is_dir()
    )


def _detect_project_root() -> Path:
    configured = os.getenv("JOB_AGENT_PROJECT_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    current = Path.cwd().resolve()
    if _looks_like_project_root(current):
        return current
    module_root = Path(__file__).resolve().parents[2]
    return module_root


PROJECT_ROOT = _detect_project_root()


def _load_dotenv(project_root: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(project_root / ".env")


@dataclass(frozen=True)
class Settings:
    project_root: Path
    profile_path: Path
    job_db_path: Path
    output_dir: Path
    private_dir: Path
    browser_sessions_dir: Path
    browser_profiles_dir: Path
    runtime_config_path: Path
    search_provider: str
    bocha_api_key: str | None = field(repr=False)
    brave_search_api_key: str | None = field(repr=False)
    map_provider: str
    map_api_key: str | None = field(repr=False)
    ai_provider: str
    ai_model: str
    ai_base_url: str | None
    ai_api_key: str | None = field(repr=False)
    has_ai_api_key: bool
    has_openai_api_key: bool
    config_source: str


def get_settings() -> Settings:
    # Resolve this at call time so the packaged desktop application can select
    # a per-user data directory before starting its local service.
    project_root = _detect_project_root()
    _load_dotenv(project_root)
    configured_profile = Path(
        os.getenv("JOB_AGENT_PROFILE", "data/private/profile.json")
    )
    if not configured_profile.is_absolute():
        configured_profile = project_root / configured_profile
    configured_job_db = Path(
        os.getenv("JOB_AGENT_DB", "data/private/job_agent.sqlite3")
    )
    if not configured_job_db.is_absolute():
        configured_job_db = project_root / configured_job_db
    configured_runtime = Path(
        os.getenv("JOB_AGENT_CONFIG", "data/private/app-settings.json")
    )
    if not configured_runtime.is_absolute():
        configured_runtime = project_root / configured_runtime
    runtime_config, config_source = effective_runtime_config(
        configured_runtime.resolve()
    )
    search_key = runtime_config.search.api_key or None
    ai_key = runtime_config.ai.api_key or None
    map_key = runtime_config.maps.api_key or None
    return Settings(
        project_root=project_root,
        profile_path=configured_profile.resolve(),
        job_db_path=configured_job_db.resolve(),
        output_dir=(project_root / "data" / "output").resolve(),
        private_dir=(project_root / "data" / "private").resolve(),
        browser_sessions_dir=(
            project_root / "data" / "private" / "browser-sessions"
        ).resolve(),
        browser_profiles_dir=(
            project_root / "data" / "private" / "browser-profiles"
        ).resolve(),
        runtime_config_path=configured_runtime.resolve(),
        search_provider=runtime_config.search.provider,
        bocha_api_key=search_key if runtime_config.search.provider == "bocha" else None,
        brave_search_api_key=search_key if runtime_config.search.provider == "brave" else None,
        map_provider=runtime_config.maps.provider,
        map_api_key=map_key if runtime_config.maps.provider == "amap" else None,
        ai_provider=runtime_config.ai.provider,
        ai_model=runtime_config.ai.model,
        ai_base_url=runtime_config.ai.base_url,
        ai_api_key=ai_key,
        has_ai_api_key=bool(ai_key),
        has_openai_api_key=(runtime_config.ai.provider == "openai" and bool(ai_key)),
        config_source=config_source,
    )
