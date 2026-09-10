from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator

from job_agent.models.base import StrictModel
from job_agent.models.profile import CommutePreferences, Profile
from job_agent.services.profile_store import load_profile, save_profile


class ProfilePreferencesError(RuntimeError):
    pass


class ProfilePreferencesUpdate(StrictModel):
    target_roles: list[str] = Field(min_length=1, max_length=12)
    adjacent_roles: list[str] = Field(default_factory=list, max_length=12)
    target_industries: list[str] = Field(default_factory=list, max_length=12)
    preferred_locations: list[str] = Field(min_length=1, max_length=12)
    employment_types: list[str] = Field(default_factory=list, max_length=8)
    must_haves: list[str] = Field(default_factory=list, max_length=12)
    avoid: list[str] = Field(default_factory=list, max_length=12)
    commute_origin: str = Field(default="", max_length=160)
    max_one_way_minutes: int | None = Field(default=None, ge=5, le=240)
    transport_modes: list[str] = Field(default_factory=list, max_length=8)
    remote_acceptable: bool = False
    internship_daily_pay_floor: int | None = Field(default=None, ge=0, le=5000)
    exclude_outsourcing: bool = True

    @field_validator(
        "target_roles",
        "adjacent_roles",
        "target_industries",
        "preferred_locations",
        "employment_types",
        "must_haves",
        "avoid",
        "transport_modes",
    )
    @classmethod
    def clean_values(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in values:
            value = item.strip()
            if not value:
                continue
            if len(value) > 80:
                raise ValueError("单个求职偏好最多 80 个字符。")
            if value not in cleaned:
                cleaned.append(value)
        return cleaned


def update_profile_preferences(
    path: Path,
    update: ProfilePreferencesUpdate,
) -> Profile:
    profile = load_profile(path)
    preferences = profile.job_search
    preferences.target_roles = update.target_roles
    preferences.adjacent_roles = update.adjacent_roles
    preferences.target_industries = update.target_industries
    preferences.preferred_locations = update.preferred_locations
    preferences.employment_types = update.employment_types
    preferences.must_haves = update.must_haves
    preferences.avoid = update.avoid
    preferences.internship_daily_pay_floor = update.internship_daily_pay_floor
    preferences.exclude_outsourcing = update.exclude_outsourcing
    preferences.commute = CommutePreferences(
        origin=update.commute_origin.strip(),
        max_one_way_minutes=update.max_one_way_minutes,
        transport_modes=update.transport_modes,
        remote_acceptable=update.remote_acceptable,
        notes=(
            "地址由用户本人填写并保存在本机；仅在本人主动点击计算路线时，"
            "发送给已配置的地图服务。"
        ),
    )
    save_profile(profile, path, overwrite=True, create_backup=True)
    return profile
