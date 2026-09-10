from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from job_agent.models.base import StrictModel


SessionMode = Literal["open_only", "safe_fill"]
SessionStatus = Literal["planned", "blocked", "completed", "failed"]
FieldCategory = Literal["safe_objective", "manual", "blocked", "ignored"]
FieldStatus = Literal["ready", "missing", "manual", "blocked", "ignored"]


class IdentityField(StrictModel):
    key: str
    label: str
    value: str | None = Field(default=None, repr=False)
    source: str
    confirmed: bool
    sensitive: bool = False


class PlannedFormField(StrictModel):
    key: str
    label: str
    category: FieldCategory
    status: FieldStatus
    value: str | None = Field(default=None, repr=False)
    source: str = ""
    sensitive: bool = False
    detail: str = ""


class BrowserHardStop(StrictModel):
    rule: str
    detail: str


class BrowserApplicationSession(StrictModel):
    schema_version: str = "1.0"
    session_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    job_id: int
    company: str
    title: str
    platform: str
    target_url: str
    allowed_domains: list[str] = Field(default_factory=list)
    mode: SessionMode = "safe_fill"
    application_pack_path: str
    resume_path: str | None = None
    resume_sha256: str | None = None
    fields: list[PlannedFormField] = Field(default_factory=list)
    hard_stops: list[BrowserHardStop] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    status: SessionStatus = "planned"
    local_demo: bool = False


class FormControlDescriptor(StrictModel):
    index: int = Field(ge=0)
    tag: str
    input_type: str = ""
    name: str = ""
    element_id: str = ""
    class_name: str = ""
    label: str = ""
    placeholder: str = ""
    aria_label: str = ""
    autocomplete: str = ""
    accept: str = ""
    action_like: bool = False
    visible: bool = True
    required: bool = False
    disabled: bool = False
    readonly: bool = False

    @property
    def hint_text(self) -> str:
        return " ".join(
            value
            for value in (
                self.label,
                self.aria_label,
                self.placeholder,
                self.name,
                self.element_id,
                self.class_name,
                self.autocomplete,
            )
            if value
        )


class ControlDecision(StrictModel):
    category: FieldCategory
    field_key: str | None = None
    reason: str


class BrowserAuditAction(StrictModel):
    action: Literal[
        "navigate",
        "fill",
        "select",
        "upload",
        "submit",
        "skip",
        "hard_stop",
        "screenshot",
    ]
    result: Literal["completed", "skipped", "blocked", "failed"]
    field_key: str | None = None
    control_hint: str = ""
    detail: str = ""
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BrowserRunReport(StrictModel):
    schema_version: str = "1.0"
    session_id: str
    started_at: datetime
    finished_at: datetime
    requested_url: str
    final_url: str
    allowed_domain: str
    status: Literal["completed", "blocked", "failed"]
    actions: list[BrowserAuditAction] = Field(default_factory=list)
    filled_fields: list[str] = Field(default_factory=list)
    uploaded_resume: bool = False
    manual_fields_detected: int = 0
    submit_controls_detected: int = 0
    submit_attempted: bool = False
    submission_prepared: bool = False
    final_confirmation_detected: bool = False
    submission_outcome: Literal[
        "not_attempted",
        "submitted",
        "form_opened",
        "secondary_confirmation",
        "unknown",
    ] = "not_attempted"
    observed_demo_submit_count: int | None = None
    observed_demo_entry_click_count: int | None = None
    screenshot_path: str | None = None
    blockers: list[str] = Field(default_factory=list)


class BrowserApplicationVerification(StrictModel):
    schema_version: str = "1.0"
    session_id: str
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    requested_url: str
    final_url: str
    observed_status: Literal[
        "applied",
        "not_applied",
        "login_required",
        "blocked",
        "unknown",
    ]
    explicit_markers: list[str] = Field(default_factory=list)
    screenshot_path: str | None = None
    blocker: str | None = None
