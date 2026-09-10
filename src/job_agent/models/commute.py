from __future__ import annotations

from typing import Literal

from pydantic import Field

from job_agent.models.base import StrictModel


CommuteRouteMode = Literal["transit", "driving", "walking", "bicycling"]


class CommuteRouteOption(StrictModel):
    rank: int = Field(ge=1, le=3)
    minutes: int = Field(ge=0, le=1440)
    distance_meters: int = Field(ge=0)
    walking_meters: int = Field(default=0, ge=0)
    transfers: int | None = Field(default=None, ge=0)
    summary: str = Field(max_length=500)


class CommuteRouteResult(StrictModel):
    provider: Literal["amap"] = "amap"
    mode: CommuteRouteMode
    origin_query: str
    origin_resolved: str
    destination_query: str
    destination_resolved: str
    options: list[CommuteRouteOption] = Field(min_length=1, max_length=3)

    @property
    def best(self) -> CommuteRouteOption:
        return self.options[0]
