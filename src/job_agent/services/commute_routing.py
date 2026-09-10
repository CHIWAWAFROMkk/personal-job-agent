from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from job_agent.models.commute import (
    CommuteRouteMode,
    CommuteRouteOption,
    CommuteRouteResult,
)


class CommuteRoutingError(RuntimeError):
    pass


JsonFetcher = Callable[[str, dict[str, str]], dict[str, object]]


@dataclass(frozen=True)
class _GeocodedPlace:
    query: str
    resolved: str
    location: str
    city: str
    citycode: str


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _items(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _number(value: object, *, default: int = 0) -> int:
    try:
        return max(0, int(float(str(value))))
    except (TypeError, ValueError):
        return default


def _trim(value: str, maximum: int = 500) -> str:
    compact = re.sub(r"\s+", " ", value).strip(" →")
    return compact if len(compact) <= maximum else compact[: maximum - 1] + "…"


class AmapCommuteProvider:
    """Calculate explainable commute estimates with 高德 Web Service APIs."""

    def __init__(self, api_key: str, *, fetch_json: JsonFetcher | None = None) -> None:
        key = api_key.strip()
        if not key:
            raise CommuteRoutingError("尚未配置高德地图 Web 服务 API Key。")
        self._api_key = key
        self._fetch_json = fetch_json or self._request_json
        self._request_count = 0

    @property
    def request_count(self) -> int:
        return self._request_count

    def _fetch(self, endpoint: str, params: dict[str, str]) -> dict[str, object]:
        payload = self._fetch_json(endpoint, params)
        self._request_count += 1
        return payload

    def _request_json(self, endpoint: str, params: dict[str, str]) -> dict[str, object]:
        query = urlencode({**params, "key": self._api_key})
        request = Request(
            f"{endpoint}?{query}",
            headers={"Accept": "application/json", "User-Agent": "PersonalJobAgent/0.8.1"},
        )
        try:
            with urlopen(request, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise CommuteRoutingError(f"高德地图服务暂时不可用（HTTP {exc.code}）。") from exc
        except (URLError, TimeoutError) as exc:
            raise CommuteRoutingError("无法连接高德地图服务，请检查网络后重试。") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CommuteRoutingError("高德地图返回了无法解析的数据。") from exc
        if not isinstance(payload, dict):
            raise CommuteRoutingError("高德地图返回格式不正确。")
        return payload

    def _geocode(self, address: str, city_hint: str = "") -> _GeocodedPlace:
        params = {"address": address, "output": "JSON"}
        if city_hint:
            params["city"] = city_hint
        payload = self._fetch(
            "https://restapi.amap.com/v3/geocode/geo",
            params,
        )
        if _text(payload.get("status")) != "1":
            detail = _text(payload.get("info")) or "地址解析失败"
            raise CommuteRoutingError(f"高德无法解析地址“{address}”：{detail}。")
        geocodes = _items(payload.get("geocodes"))
        if not geocodes:
            raise CommuteRoutingError(
                f"没有找到地址“{address}”。请补充城市、区县、道路或地标名称。"
            )
        selected = geocodes[0]
        location = _text(selected.get("location"))
        if not re.fullmatch(r"-?\d{1,3}(?:\.\d{1,6})?,-?\d{1,2}(?:\.\d{1,6})?", location):
            raise CommuteRoutingError(f"地址“{address}”没有返回有效坐标。")
        city = _text(selected.get("city")) or city_hint
        return _GeocodedPlace(
            query=address,
            resolved=_text(selected.get("formatted_address")) or address,
            location=location,
            city=city,
            citycode=_text(selected.get("citycode")),
        )

    def calculate(
        self,
        *,
        origin_address: str,
        destination_address: str,
        mode: CommuteRouteMode = "transit",
        origin_city: str = "",
        destination_city: str = "",
    ) -> CommuteRouteResult:
        origin_address = origin_address.strip()
        destination_address = destination_address.strip()
        if not origin_address or len(origin_address) > 160:
            raise CommuteRoutingError("请填写 1-160 个字符的常用出发地址。")
        if not destination_address or len(destination_address) > 200:
            raise CommuteRoutingError("请填写 1-200 个字符的办公地址。")
        if mode not in {"transit", "driving", "walking", "bicycling"}:
            raise CommuteRoutingError("暂不支持这种通勤方式。")

        origin = self._geocode(origin_address, origin_city.strip())
        destination = self._geocode(destination_address, destination_city.strip())
        options = self._route(origin, destination, mode)
        return CommuteRouteResult(
            mode=mode,
            origin_query=origin.query,
            origin_resolved=origin.resolved,
            destination_query=destination.query,
            destination_resolved=destination.resolved,
            options=options,
        )

    def _route(
        self,
        origin: _GeocodedPlace,
        destination: _GeocodedPlace,
        mode: CommuteRouteMode,
    ) -> list[CommuteRouteOption]:
        common = {
            "origin": origin.location,
            "destination": destination.location,
            "output": "JSON",
        }
        if mode == "transit":
            city = origin.citycode or origin.city
            if not city:
                raise CommuteRoutingError("公交/地铁路线需要能够识别出发城市。")
            payload = self._fetch(
                "https://restapi.amap.com/v3/direction/transit/integrated",
                {**common, "city": city, "cityd": destination.citycode or destination.city, "strategy": "0", "extensions": "all"},
            )
            self._ensure_v3_success(payload)
            route = payload.get("route") if isinstance(payload.get("route"), dict) else {}
            raw_options = _items(route.get("transits"))
            parsed = [self._transit_option(item, index + 1) for index, item in enumerate(raw_options)]
        elif mode in {"driving", "walking"}:
            endpoint = f"https://restapi.amap.com/v3/direction/{mode}"
            params = dict(common)
            if mode == "driving":
                params.update({"strategy": "10", "extensions": "all"})
            payload = self._fetch(endpoint, params)
            self._ensure_v3_success(payload)
            route = payload.get("route") if isinstance(payload.get("route"), dict) else {}
            parsed = [
                self._path_option(item, index + 1, mode)
                for index, item in enumerate(_items(route.get("paths")))
            ]
        else:
            payload = self._fetch(
                "https://restapi.amap.com/v4/direction/bicycling",
                common,
            )
            if _text(payload.get("errcode")) not in {"", "0"}:
                raise CommuteRoutingError(
                    f"高德骑行路线计算失败：{_text(payload.get('errmsg')) or '未知错误'}。"
                )
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            parsed = [
                self._path_option(item, index + 1, mode)
                for index, item in enumerate(_items(data.get("paths")))
            ]

        usable = [item for item in parsed if item.minutes > 0]
        if not usable:
            raise CommuteRoutingError("没有找到可用通勤路线，请确认起点、办公地址和交通方式。")
        usable.sort(key=lambda item: (item.minutes, item.distance_meters))
        return [item.model_copy(update={"rank": index + 1}) for index, item in enumerate(usable[:3])]

    @staticmethod
    def _ensure_v3_success(payload: dict[str, object]) -> None:
        if _text(payload.get("status")) != "1":
            detail = _text(payload.get("info")) or "未知错误"
            raise CommuteRoutingError(f"高德路线计算失败：{detail}。")

    @staticmethod
    def _path_option(
        path: dict[str, object], rank: int, mode: CommuteRouteMode
    ) -> CommuteRouteOption:
        duration = _number(path.get("duration"))
        distance = _number(path.get("distance"))
        instructions = [
            _text(step.get("instruction"))
            for step in _items(path.get("steps"))
            if _text(step.get("instruction"))
        ]
        mode_label = {"driving": "驾车", "walking": "步行", "bicycling": "骑行"}[mode]
        summary = " → ".join(instructions[:4]) or f"{mode_label}推荐路线"
        return CommuteRouteOption(
            rank=rank,
            minutes=math.ceil(duration / 60) if duration else 0,
            distance_meters=distance,
            walking_meters=distance if mode == "walking" else 0,
            summary=_trim(summary),
        )

    @staticmethod
    def _transit_option(transit: dict[str, object], rank: int) -> CommuteRouteOption:
        duration = _number(transit.get("duration"))
        walking = _number(transit.get("walking_distance"))
        parts: list[str] = []
        line_distance = 0
        line_count = 0
        for segment in _items(transit.get("segments")):
            walking_data = segment.get("walking")
            if isinstance(walking_data, dict):
                segment_walk = _number(walking_data.get("distance"))
                if segment_walk >= 150:
                    parts.append(f"步行 {segment_walk} 米")
            bus = segment.get("bus") if isinstance(segment.get("bus"), dict) else {}
            for line in _items(bus.get("buslines"))[:1]:
                name = _text(line.get("name")).split("(", 1)[0]
                departure = line.get("departure_stop") if isinstance(line.get("departure_stop"), dict) else {}
                arrival = line.get("arrival_stop") if isinstance(line.get("arrival_stop"), dict) else {}
                station_text = ""
                if _text(departure.get("name")) and _text(arrival.get("name")):
                    station_text = f"（{_text(departure.get('name'))}→{_text(arrival.get('name'))}）"
                if name:
                    parts.append(name + station_text)
                    line_count += 1
                line_distance += _number(line.get("distance"))
            railway = segment.get("railway") if isinstance(segment.get("railway"), dict) else {}
            railway_name = _text(railway.get("name"))
            if railway_name:
                parts.append(railway_name)
                line_count += 1
                line_distance += _number(railway.get("distance"))
        distance = line_distance + walking
        summary = " → ".join(parts) or "公交/地铁最快方案"
        return CommuteRouteOption(
            rank=rank,
            minutes=math.ceil(duration / 60) if duration else 0,
            distance_meters=distance,
            walking_meters=walking,
            transfers=max(0, line_count - 1) if line_count else None,
            summary=_trim(summary),
        )
