from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from job_agent.models.base import StrictModel


CopilotRole = Literal["user", "assistant"]
CopilotActionKind = Literal[
    "review_queue",
    "open_job",
    "open_preparation",
    "open_profile",
    "open_settings",
    "create_resume_draft",
    "revise_resume",
    "approve_resume_draft",
    "start_safe_fill",
]


class CopilotAction(StrictModel):
    action_id: str
    label: str
    kind: CopilotActionKind
    job_id: int | None = None
    requires_confirmation: bool = False
    description: str = ""
    instruction: str = ""


class CopilotMessage(StrictModel):
    message_id: str
    role: CopilotRole
    content: str
    job_id: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provider: str = "local"
    actions: list[CopilotAction] = Field(default_factory=list)


class CopilotThread(StrictModel):
    schema_version: str = "phase12-copilot-v1"
    thread_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    messages: list[CopilotMessage] = Field(default_factory=list)


class CopilotSnapshot(StrictModel):
    thread: CopilotThread
    provider: str
    model: str
    suggestions: list[str] = Field(default_factory=list)
    privacy_note: str = (
        "对话只读取脱敏后的本地求职上下文；联系方式、API Key 和私有文件路径不会发送给模型。"
    )
    safety_note: str = (
        "对话可以建议并准备动作，但网页登录、填写、上传与最终提交均由本人完成。"
    )
