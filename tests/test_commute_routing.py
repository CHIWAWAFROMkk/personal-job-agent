from __future__ import annotations

import unittest

from job_agent.services.commute_routing import AmapCommuteProvider, CommuteRoutingError


def geocode(address: str) -> dict[str, object]:
    return {
        "status": "1",
        "geocodes": [
            {
                "formatted_address": f"已解析：{address}",
                "location": "121.433,31.188",
                "city": "上海市",
                "citycode": "021",
            }
        ],
    }


class CommuteRoutingTests(unittest.TestCase):
    def test_transit_routes_are_sorted_and_explainable(self) -> None:
        calls: list[tuple[str, dict[str, str]]] = []

        def fetch(endpoint: str, params: dict[str, str]) -> dict[str, object]:
            calls.append((endpoint, params))
            if "geocode/geo" in endpoint:
                return geocode(params["address"])
            return {
                "status": "1",
                "route": {
                    "transits": [
                        {
                            "duration": "3120",
                            "walking_distance": "600",
                            "segments": [
                                {
                                    "bus": {
                                        "buslines": [
                                            {
                                                "name": "地铁9号线(曹路--松江南站)",
                                                "distance": "13000",
                                                "departure_stop": {"name": "宜山路"},
                                                "arrival_stop": {"name": "陆家浜路"},
                                            }
                                        ]
                                    }
                                }
                            ],
                        },
                        {
                            "duration": "2701",
                            "walking_distance": "720",
                            "segments": [
                                {
                                    "bus": {
                                        "buslines": [
                                            {
                                                "name": "地铁10号线",
                                                "distance": "14200",
                                                "departure_stop": {"name": "虹桥路"},
                                                "arrival_stop": {"name": "江湾体育场"},
                                            }
                                        ]
                                    }
                                }
                            ],
                        },
                    ]
                },
            }

        provider = AmapCommuteProvider("secret", fetch_json=fetch)
        result = provider.calculate(
            origin_address="上海市徐汇区宜山路站",
            destination_address="上海市杨浦区创智天地",
            mode="transit",
        )

        self.assertEqual(result.best.minutes, 46)
        self.assertIn("地铁10号线", result.best.summary)
        self.assertEqual(provider.request_count, 3)
        self.assertEqual(len(calls), 3)

    def test_driving_route_parses_distance_and_steps(self) -> None:
        def fetch(endpoint: str, params: dict[str, str]) -> dict[str, object]:
            if "geocode/geo" in endpoint:
                return geocode(params["address"])
            return {
                "status": "1",
                "route": {
                    "paths": [
                        {
                            "duration": "1801",
                            "distance": "22000",
                            "steps": [{"instruction": "沿内环高架行驶"}],
                        }
                    ]
                },
            }

        result = AmapCommuteProvider("secret", fetch_json=fetch).calculate(
            origin_address="徐汇区宜山路",
            destination_address="杨浦区创智天地",
            mode="driving",
        )

        self.assertEqual(result.best.minutes, 31)
        self.assertEqual(result.best.distance_meters, 22000)
        self.assertIn("内环高架", result.best.summary)

    def test_unresolved_address_has_clear_error(self) -> None:
        def fetch(endpoint: str, params: dict[str, str]) -> dict[str, object]:
            return {"status": "0", "info": "INVALID_PARAMS"}

        with self.assertRaisesRegex(CommuteRoutingError, "无法解析地址"):
            AmapCommuteProvider("secret", fetch_json=fetch).calculate(
                origin_address="不存在的地址",
                destination_address="创智天地",
            )


if __name__ == "__main__":
    unittest.main()
