from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from job_agent.models.base import StrictModel
from job_agent.services.profile_store import write_json_atomic


AIProviderId = Literal["local", "openai", "openai_compatible"]
SearchProviderId = Literal["none", "bocha", "brave"]
MapProviderId = Literal["none", "amap"]


class RuntimeConfigError(RuntimeError):
    pass


def _clean_secret(value: str) -> str:
    cleaned = value.strip()
    if "\n" in cleaned or "\r" in cleaned:
        raise ValueError("API Key 不能包含换行。")
    if len(cleaned) > 4096:
        raise ValueError("API Key 长度异常。")
    return cleaned


def _validate_base_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    cleaned = value.strip().rstrip("/")
    parsed = urlsplit(cleaned)
    hostname = (parsed.hostname or "").casefold()
    loopback = hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme not in ({"http", "https"} if loopback else {"https"}):
        raise ValueError("自定义 AI 地址必须使用 HTTPS；仅本机地址允许 HTTP。")
    if not hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("自定义 AI 地址格式不正确。")
    return cleaned


class AIConnectorConfig(StrictModel):
    provider: AIProviderId = "local"
    model: str = Field(default="local-explainable-v1", max_length=160)
    base_url: str | None = None
    api_key: str = Field(default="", max_length=4096, repr=False)
    monthly_quota: int | None = Field(default=None, ge=1, le=10000000)

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: str) -> str:
        return _clean_secret(value)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        return _validate_base_url(value)

    @model_validator(mode="before")
    @classmethod
    def normalize_provider_fields(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        provider = str(normalized.get("provider", "local")).strip().casefold()
        if provider == "local":
            normalized["model"] = str(normalized.get("model") or "local-explainable-v1")
            normalized["base_url"] = None
            normalized["api_key"] = ""
        elif provider == "openai":
            normalized["base_url"] = None
        return normalized

    @model_validator(mode="after")
    def validate_provider_requirements(self) -> "AIConnectorConfig":
        if self.provider == "local":
            return self
        if not self.model.strip():
            raise ValueError("使用云端 AI 时必须填写模型名称。")
        if self.provider == "openai_compatible" and not self.base_url:
            raise ValueError("OpenAI 兼容 API 必须填写服务地址。")
        return self


class SearchConnectorConfig(StrictModel):
    provider: SearchProviderId = "none"
    api_key: str = Field(default="", max_length=4096, repr=False)
    monthly_quota: int | None = Field(default=None, ge=1, le=10000000)

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: str) -> str:
        return _clean_secret(value)

    @model_validator(mode="before")
    @classmethod
    def clear_disabled_secret(cls, value: object) -> object:
        if isinstance(value, dict) and str(value.get("provider", "none")).casefold() == "none":
            normalized = dict(value)
            normalized["api_key"] = ""
            return normalized
        return value


class MapConnectorConfig(StrictModel):
    provider: MapProviderId = "none"
    api_key: str = Field(default="", max_length=4096, repr=False)
    monthly_quota: int | None = Field(default=None, ge=1, le=10000000)

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: str) -> str:
        return _clean_secret(value)

    @model_validator(mode="before")
    @classmethod
    def clear_disabled_secret(cls, value: object) -> object:
        if isinstance(value, dict) and str(value.get("provider", "none")).casefold() == "none":
            normalized = dict(value)
            normalized["api_key"] = ""
            return normalized
        return value


class RuntimeConfig(StrictModel):
    schema_version: str = "1"
    ai: AIConnectorConfig = Field(default_factory=AIConnectorConfig)
    search: SearchConnectorConfig = Field(default_factory=SearchConnectorConfig)
    maps: MapConnectorConfig = Field(default_factory=MapConnectorConfig)


class RuntimeConfigUpdate(StrictModel):
    ai_provider: AIProviderId
    ai_model: str = Field(default="", max_length=160)
    ai_base_url: str | None = Field(default=None, max_length=500)
    ai_api_key: str = Field(default="", max_length=4096, repr=False)
    clear_ai_api_key: bool = False
    ai_monthly_quota: int | None = Field(default=None, ge=1, le=10000000)
    search_provider: SearchProviderId
    search_api_key: str = Field(default="", max_length=4096, repr=False)
    clear_search_api_key: bool = False
    search_monthly_quota: int | None = Field(default=None, ge=1, le=10000000)
    map_provider: MapProviderId = "none"
    map_api_key: str = Field(default="", max_length=4096, repr=False)
    clear_map_api_key: bool = False
    map_monthly_quota: int | None = Field(default=None, ge=1, le=10000000)

    @field_validator("ai_api_key", "search_api_key", "map_api_key")
    @classmethod
    def validate_api_keys(cls, value: str) -> str:
        return _clean_secret(value)


def load_runtime_config(path: Path) -> RuntimeConfig:
    if not path.is_file():
        return RuntimeConfig()
    try:
        return RuntimeConfig.model_validate_json(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeConfigError(f"本地 API 配置无法读取: {exc}") from exc


def save_runtime_config(config: RuntimeConfig, path: Path) -> Path:
    try:
        return write_json_atomic(config.model_dump(mode="json"), path)
    except OSError as exc:
        raise RuntimeConfigError(f"本地 API 配置无法保存: {exc}") from exc


def update_runtime_config(path: Path, update: RuntimeConfigUpdate) -> RuntimeConfig:
    existing = load_runtime_config(path)
    ai_key = update.ai_api_key
    if not ai_key and not update.clear_ai_api_key and existing.ai.provider == update.ai_provider:
        ai_key = existing.ai.api_key
    search_key = update.search_api_key
    if (
        not search_key
        and not update.clear_search_api_key
        and existing.search.provider == update.search_provider
    ):
        search_key = existing.search.api_key
    map_key = update.map_api_key
    if (
        not map_key
        and not update.clear_map_api_key
        and existing.maps.provider == update.map_provider
    ):
        map_key = existing.maps.api_key
    config = RuntimeConfig(
        ai=AIConnectorConfig(
            provider=update.ai_provider,
            model=update.ai_model or (
                "local-explainable-v1" if update.ai_provider == "local" else ""
            ),
            base_url=update.ai_base_url,
            api_key=ai_key,
            monthly_quota=update.ai_monthly_quota,
        ),
        search=SearchConnectorConfig(
            provider=update.search_provider,
            api_key=search_key,
            monthly_quota=update.search_monthly_quota,
        ),
        maps=MapConnectorConfig(
            provider=update.map_provider,
            api_key=map_key,
            monthly_quota=update.map_monthly_quota,
        ),
    )
    save_runtime_config(config, path)
    return config


def _environment_runtime_config() -> RuntimeConfig:
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    ai_provider = os.getenv("JOB_AGENT_AI_PROVIDER", "").strip().casefold()
    if ai_provider not in {"local", "openai", "openai_compatible"}:
        ai_provider = "openai" if openai_key else "local"
    ai_model = os.getenv("JOB_AGENT_AI_MODEL", os.getenv("OPENAI_MODEL", "")).strip()
    if not ai_model:
        ai_model = "gpt-5.6-luna" if ai_provider != "local" else "local-explainable-v1"
    ai_key = os.getenv("JOB_AGENT_AI_API_KEY", "").strip() or openai_key
    ai_base_url = os.getenv("JOB_AGENT_AI_BASE_URL", "").strip() or None

    bocha_key = os.getenv("BOCHA_API_KEY", "").strip()
    brave_key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
    search_provider = os.getenv("JOB_AGENT_SEARCH_PROVIDER", "").strip().casefold()
    if search_provider not in {"none", "bocha", "brave"}:
        search_provider = "bocha" if bocha_key else "brave" if brave_key else "none"
    search_key = bocha_key if search_provider == "bocha" else brave_key if search_provider == "brave" else ""
    amap_key = os.getenv("AMAP_WEB_API_KEY", "").strip() or os.getenv(
        "JOB_AGENT_MAP_API_KEY", ""
    ).strip()
    map_provider = os.getenv("JOB_AGENT_MAP_PROVIDER", "").strip().casefold()
    if map_provider not in {"none", "amap"}:
        map_provider = "amap" if amap_key else "none"
    try:
        return RuntimeConfig(
            ai=AIConnectorConfig(
                provider=ai_provider,  # type: ignore[arg-type]
                model=ai_model,
                base_url=ai_base_url,
                api_key=ai_key,
            ),
            search=SearchConnectorConfig(
                provider=search_provider,  # type: ignore[arg-type]
                api_key=search_key,
            ),
            maps=MapConnectorConfig(
                provider=map_provider,  # type: ignore[arg-type]
                api_key=amap_key,
            ),
        )
    except ValueError as exc:
        raise RuntimeConfigError(f"环境变量中的 API 配置无效: {exc}") from exc


def effective_runtime_config(path: Path) -> tuple[RuntimeConfig, str]:
    environment = _environment_runtime_config()
    if not path.is_file():
        return environment, "environment" if (
            environment.ai.api_key
            or environment.search.api_key
            or environment.maps.api_key
        ) else "defaults"
    local = load_runtime_config(path)
    ai_key = local.ai.api_key
    if not ai_key:
        if local.ai.provider in {"openai", "openai_compatible"}:
            ai_key = os.getenv("JOB_AGENT_AI_API_KEY", "").strip() or os.getenv(
                "OPENAI_API_KEY", ""
            ).strip()
    search_key = local.search.api_key
    if not search_key:
        if local.search.provider == "bocha":
            search_key = os.getenv("BOCHA_API_KEY", "").strip()
        elif local.search.provider == "brave":
            search_key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
    map_key = local.maps.api_key
    if not map_key and local.maps.provider == "amap":
        map_key = os.getenv("AMAP_WEB_API_KEY", "").strip() or os.getenv(
            "JOB_AGENT_MAP_API_KEY", ""
        ).strip()
    return (
        RuntimeConfig(
            ai=local.ai.model_copy(update={"api_key": ai_key}),
            search=local.search.model_copy(update={"api_key": search_key}),
            maps=local.maps.model_copy(update={"api_key": map_key}),
        ),
        "local_file",
    )


def public_runtime_config(
    path: Path,
    *,
    usage_path: Path | None = None,
) -> dict[str, object]:
    config, source = effective_runtime_config(path)
    ai_host = urlsplit(config.ai.base_url).hostname if config.ai.base_url else None
    local_compatible = config.ai.provider == "openai_compatible" and ai_host in {
        "127.0.0.1",
        "localhost",
        "::1",
    }
    ai_ready = config.ai.provider == "local" or bool(config.ai.api_key) or local_compatible
    search_enabled = config.search.provider != "none"
    maps_enabled = config.maps.provider != "none"
    public: dict[str, object] = {
        "schema_version": config.schema_version,
        "source": source,
        "ai": {
            "provider": config.ai.provider,
            "model": config.ai.model,
            "base_url": config.ai.base_url,
            "api_key_configured": bool(config.ai.api_key),
            "ready": ai_ready,
            "monthly_quota": config.ai.monthly_quota,
        },
        "search": {
            "provider": config.search.provider,
            "api_key_configured": bool(config.search.api_key),
            "enabled": search_enabled,
            "ready": not search_enabled or bool(config.search.api_key),
            "monthly_quota": config.search.monthly_quota,
        },
        "maps": {
            "provider": config.maps.provider,
            "api_key_configured": bool(config.maps.api_key),
            "enabled": maps_enabled,
            "ready": not maps_enabled or bool(config.maps.api_key),
            "monthly_quota": config.maps.monthly_quota,
        },
        "supported": {
            "ai": ["local", "openai", "openai_compatible"],
            "search": ["none", "bocha", "brave"],
            "maps": ["none", "amap"],
        },
        "privacy": "密钥只保存在本机，不会通过 Dashboard 接口回显。",
    }
    if usage_path is not None:
        from job_agent.services.api_usage import public_api_usage

        public["usage"] = public_api_usage(
            usage_path,
            {
                "ai": config.ai.monthly_quota,
                "search": config.search.monthly_quota,
                "maps": config.maps.monthly_quota,
            },
        )
    return public
