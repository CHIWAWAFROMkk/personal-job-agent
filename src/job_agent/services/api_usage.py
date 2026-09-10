from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from job_agent.models.base import StrictModel
from job_agent.services.profile_store import write_json_atomic


ConnectorKind = Literal["ai", "search", "maps"]


class ApiUsageCounter(StrictModel):
    provider: str = ""
    successful_requests: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    last_used_at: str | None = None


class MonthlyApiUsage(StrictModel):
    schema_version: str = "1"
    month: str
    ai: ApiUsageCounter = Field(default_factory=ApiUsageCounter)
    search: ApiUsageCounter = Field(default_factory=ApiUsageCounter)
    maps: ApiUsageCounter = Field(default_factory=ApiUsageCounter)


def _month_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def load_api_usage(path: Path) -> MonthlyApiUsage:
    month = _month_now()
    if not path.is_file():
        return MonthlyApiUsage(month=month)
    try:
        usage = MonthlyApiUsage.model_validate_json(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return MonthlyApiUsage(month=month)
    if usage.month != month:
        return MonthlyApiUsage(month=month)
    return usage


def record_api_usage(
    path: Path,
    connector: ConnectorKind,
    provider: str,
    *,
    successful_requests: int = 1,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> MonthlyApiUsage:
    if successful_requests < 0 or input_tokens < 0 or output_tokens < 0:
        raise ValueError("API 用量不能为负数。")
    usage = load_api_usage(path)
    counter = getattr(usage, connector)
    updated = counter.model_copy(
        update={
            "provider": provider.strip(),
            "successful_requests": counter.successful_requests + successful_requests,
            "input_tokens": counter.input_tokens + input_tokens,
            "output_tokens": counter.output_tokens + output_tokens,
            "last_used_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    )
    setattr(usage, connector, updated)
    write_json_atomic(usage.model_dump(mode="json"), path)
    return usage


def public_api_usage(path: Path, limits: dict[ConnectorKind, int | None]) -> dict[str, object]:
    usage = load_api_usage(path)
    connectors: dict[str, object] = {}
    for connector in ("ai", "search", "maps"):
        counter = getattr(usage, connector)
        limit = limits.get(connector)  # type: ignore[arg-type]
        remaining = (
            max(0, limit - counter.successful_requests)
            if limit is not None
            else None
        )
        connectors[connector] = {
            "provider": counter.provider,
            "successful_requests": counter.successful_requests,
            "input_tokens": counter.input_tokens,
            "output_tokens": counter.output_tokens,
            "last_used_at": counter.last_used_at,
            "local_monthly_limit": limit,
            "local_remaining": remaining,
            "balance_source": "local_limit" if limit is not None else "provider_console",
        }
    return {
        "month": usage.month,
        "connectors": connectors,
        "notice": (
            "这里只统计本软件成功发出的请求。服务商账户真实余额以其控制台为准；"
            "设置本机月度上限后可显示可控剩余额度。"
        ),
    }
