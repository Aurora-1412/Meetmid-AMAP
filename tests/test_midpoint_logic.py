import unittest
from unittest.mock import patch

import amap_client


class AdaptiveMidpointTest(unittest.TestCase):
    def test_mixed_or_walking_preferences_force_geo(self):
        self.assertTrue(amap_client.should_force_geo_midpoint("transit", "driving"))
        self.assertTrue(amap_client.should_force_geo_midpoint("walking", "walking"))
        self.assertFalse(amap_client.should_force_geo_midpoint("auto", "auto"))
        self.assertFalse(amap_client.should_force_geo_midpoint("transit", "transit"))

    def test_forced_geo_does_not_call_network_checks(self):
        with patch.object(amap_client, "inspect_water_or_sea_point") as water_check:
            result = amap_client.find_adaptive_midpoint(
                116.0, 39.0, 117.0, 40.0,
                prefer_a="walking",
                prefer_b="walking",
            )

        self.assertEqual(result["method"], "geo")
        water_check.assert_not_called()

    def test_geo_used_when_transit_time_is_balanced(self):
        with (
            patch.object(
                amap_client,
                "inspect_water_or_sea_point",
                return_value={"success": True, "is_water": False},
            ),
            patch.object(
                amap_client,
                "transit_duration_to_point",
                side_effect=[
                    {"success": True, "duration_minutes": 35},
                    {"success": True, "duration_minutes": 50},
                ],
            ),
            patch.object(amap_client, "find_transit_route_subway_midpoint") as station_midpoint,
        ):
            result = amap_client.find_adaptive_midpoint(
                116.0, 39.0, 117.0, 40.0,
                prefer_a="transit",
                prefer_b="transit",
            )

        self.assertEqual(result["method"], "geo")
        self.assertEqual(result["geo_transit_time_diff_minutes"], 15)
        station_midpoint.assert_not_called()

    def test_large_transit_time_gap_uses_balanced_station(self):
        station_result = {
            "success": True,
            "method": "transit_route_subway_midpoint",
            "midpoint": {"lng": 116.4, "lat": 39.9},
            "suggested_search_radius_m": 1200,
        }
        with (
            patch.object(
                amap_client,
                "inspect_water_or_sea_point",
                return_value={"success": True, "is_water": False},
            ),
            patch.object(
                amap_client,
                "transit_duration_to_point",
                side_effect=[
                    {"success": True, "duration_minutes": 20},
                    {"success": True, "duration_minutes": 65},
                ],
            ),
            patch.object(
                amap_client,
                "find_transit_route_subway_midpoint",
                return_value=station_result,
            ),
        ):
            result = amap_client.find_adaptive_midpoint(
                116.0, 39.0, 117.0, 40.0,
                prefer_a="auto",
                prefer_b="auto",
            )

        self.assertEqual(result["method"], "transit_route_subway_midpoint")
        self.assertIn("公共交通时间差 45 分钟", result["decision_reason"])

    def test_subway_route_midpoint_uses_half_before_and_after_stations(self):
        transit = {
            "duration": "1200",
            "segments": [
                {
                    "walking": {"duration": "0"},
                    "bus": {
                        "buslines": [
                            {
                                "name": "地铁10号线",
                                "duration": "1200",
                                "departure_stop": {
                                    "name": "A站",
                                    "location": "116.0,39.0",
                                },
                                "via_stops": [
                                    {"name": "B站", "location": "116.2,39.2"},
                                    {"name": "C站", "location": "116.4,39.4"},
                                    {"name": "D站", "location": "116.6,39.6"},
                                ],
                                "arrival_stop": {
                                    "name": "E站",
                                    "location": "116.8,39.8",
                                },
                            }
                        ]
                    },
                }
            ],
        }
        stations = amap_client._build_subway_station_timeline(transit)

        self.assertEqual([station["name"] for station in stations], ["A站", "B站", "C站", "D站", "E站"])
        self.assertEqual(round(stations[2]["cum_seconds"]), 600)

    def test_transit_route_subway_midpoint_returns_geo_midpoint_of_station_pair(self):
        class FakeResponse:
            def json(self):
                return {
                    "status": "1",
                    "route": {
                        "transits": [
                            {
                                "duration": "1200",
                                "segments": [
                                    {
                                        "walking": {"duration": "0"},
                                        "bus": {
                                            "buslines": [
                                                {
                                                    "name": "地铁10号线",
                                                    "duration": "1200",
                                                    "departure_stop": {
                                                        "name": "A站",
                                                        "location": "116.0,39.0",
                                                    },
                                                    "via_stops": [
                                                        {"name": "B站", "location": "116.2,39.2"},
                                                        {"name": "C站", "location": "116.4,39.4"},
                                                        {"name": "D站", "location": "116.6,39.6"},
                                                    ],
                                                    "arrival_stop": {
                                                        "name": "E站",
                                                        "location": "116.8,39.8",
                                                    },
                                                }
                                            ]
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                }

        with patch.object(amap_client.requests, "get", return_value=FakeResponse()):
            result = amap_client.find_transit_route_subway_midpoint(
                116.0, 39.0, 117.0, 40.0,
                geo_midpoint={
                    "midpoint": {"lng": 116.5, "lat": 39.5},
                    "total_distance_km": 100,
                    "suggested_search_radius_m": 3000,
                },
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["method"], "transit_route_subway_midpoint")
        self.assertEqual(result["station_pair"], ["C站", "D站"])
        self.assertAlmostEqual(result["midpoint"]["lng"], 116.5)
        self.assertAlmostEqual(result["midpoint"]["lat"], 39.5)


if __name__ == "__main__":
    unittest.main()
