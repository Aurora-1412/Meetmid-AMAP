import unittest

import amap_client
from app import amap_client as deployment_amap_client


class RouteGeometryTest(unittest.TestCase):
    def test_parse_polyline_drops_invalid_and_consecutive_duplicate_points(self):
        self.assertEqual(
            amap_client.parse_polyline(
                "116.1,39.9;bad;116.1,39.9;116.2,40.0"
            ),
            [[116.1, 39.9], [116.2, 40.0]],
        )

    def test_step_geometry_keeps_each_non_empty_step(self):
        geometry = amap_client.build_step_geometry(
            [
                {
                    "instruction": "向东",
                    "distance": "100",
                    "duration": "60",
                    "polyline": "116.1,39.9;116.2,40.0",
                },
                {
                    "instruction": "无轨迹",
                    "distance": "0",
                    "duration": "0",
                    "polyline": "",
                },
            ],
            "driving",
        )

        self.assertEqual(
            geometry,
            [{
                "mode": "driving",
                "path": [[116.1, 39.9], [116.2, 40.0]],
            }],
        )

    def test_transit_geometry_contains_walking_and_bus_segments(self):
        transit = {
            "duration": "1200",
            "segments": [{
                "walking": {
                    "distance": "300",
                    "duration": "240",
                    "steps": [{
                        "instruction": "步行到车站",
                        "polyline": "116.0,39.0;116.01,39.01",
                    }],
                },
                "bus": {
                    "buslines": [{
                        "name": "地铁1号线",
                        "distance": "5000",
                        "duration": "900",
                        "polyline": "116.01,39.01;116.3,39.2",
                        "departure_stop": {
                            "name": "甲站",
                            "location": "116.01,39.01",
                        },
                        "arrival_stop": {
                            "name": "乙站",
                            "location": "116.3,39.2",
                        },
                        "via_stops": [],
                    }]
                },
            }],
        }

        self.assertEqual(
            amap_client.build_transit_geometry(transit),
            {
                "mode": "transit",
                "segments": [
                    {
                        "mode": "walking",
                        "path": [[116.0, 39.0], [116.01, 39.01]],
                    },
                    {
                        "mode": "transit",
                        "path": [[116.01, 39.01], [116.3, 39.2]],
                    },
                ],
            },
        )

    def test_empty_geometry_returns_none(self):
        self.assertIsNone(amap_client.build_route_geometry("walking", []))

    def test_deployment_adapter_exposes_the_same_geometry_contract(self):
        self.assertEqual(
            deployment_amap_client.build_route_geometry(
                "driving",
                deployment_amap_client.build_step_geometry(
                    [{"polyline": "116.1,39.9;116.2,40.0"}],
                    "driving",
                ),
            ),
            {
                "mode": "driving",
                "segments": [{
                    "mode": "driving",
                    "path": [[116.1, 39.9], [116.2, 40.0]],
                }],
            },
        )


if __name__ == "__main__":
    unittest.main()
