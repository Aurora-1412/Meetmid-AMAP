import unittest
from unittest.mock import patch

import app_v2
from app import amap_client as deployment_amap_client


class MidpointSelectionTest(unittest.TestCase):
    def test_geo_mode_keeps_independent_transport_preferences(self):
        error = app_v2.validate_midpoint_choice("geo", "walking", "driving")
        self.assertIsNone(error)

        result = app_v2.compute_requested_midpoint(
            116.0,
            39.0,
            118.0,
            41.0,
            midpoint_mode="geo",
            prefer_a="walking",
            prefer_b="driving",
            city="北京",
            departure_time=None,
        )

        self.assertEqual(result["method"], "geo")
        self.assertEqual(result["midpoint"], {"lng": 117.0, "lat": 40.0})

    def test_transport_mode_rejects_mixed_or_non_transport_preferences(self):
        cases = [
            ("transit", "driving"),
            ("walking", "walking"),
            ("auto", "auto"),
        ]
        for prefer_a, prefer_b in cases:
            with self.subTest(prefer_a=prefer_a, prefer_b=prefer_b):
                error = app_v2.validate_midpoint_choice(
                    "transport", prefer_a, prefer_b
                )
                self.assertEqual(
                    error,
                    "选择交通中点时，双方必须同时选择公交地铁或同时选择驾车",
                )

    def test_transport_mode_uses_the_selected_time_midpoint_algorithm(self):
        def fake_time_midpoint(*args, mode, **kwargs):
            return {
                "method": f"{mode}_time",
                "midpoint": {"lng": 116.5, "lat": 39.5},
                "suggested_search_radius_m": 1800,
            }

        with patch.object(
            app_v2, "find_time_midpoint", side_effect=fake_time_midpoint
        ):
            transit = app_v2.compute_requested_midpoint(
                116.0,
                39.0,
                117.0,
                40.0,
                midpoint_mode="transport",
                prefer_a="transit",
                prefer_b="transit",
                city="北京",
                departure_time="09:30",
            )
            driving = app_v2.compute_requested_midpoint(
                116.0,
                39.0,
                117.0,
                40.0,
                midpoint_mode="transport",
                prefer_a="driving",
                prefer_b="driving",
                city="北京",
                departure_time=None,
            )

        self.assertEqual(transit["method"], "transit_time")
        self.assertEqual(driving["method"], "driving_time")

    def test_search_endpoint_rejects_invalid_transport_midpoint_before_pipeline(self):
        client = app_v2.app.test_client()
        response = client.post(
            "/api/v2/search",
            json={
                "location_a": {"lng": 116.0, "lat": 39.0, "name": "A"},
                "location_b": {"lng": 117.0, "lat": 40.0, "name": "B"},
                "query": "咖啡馆",
                "midpoint_mode": "transport",
                "prefer_a": "transit",
                "prefer_b": "driving",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"],
            "选择交通中点时，双方必须同时选择公交地铁或同时选择驾车",
        )

    def test_deployment_adapter_computes_driving_time_midpoint(self):
        response = {
            "status": "1",
            "route": {
                "paths": [{
                    "duration": "600",
                    "steps": [{
                        "duration": "600",
                        "polyline": (
                            "116.0,39.0;116.4,39.4;"
                            "116.7,39.7;117.0,40.0"
                        ),
                    }],
                }],
            },
        }
        with patch.object(
            deployment_amap_client, "_amap_get", return_value=response
        ):
            result = deployment_amap_client.find_time_midpoint(
                116.0, 39.0, 117.0, 40.0, mode="driving"
            )

        self.assertEqual(result["method"], "driving_time")
        self.assertEqual(result["midpoint"], {"lng": 116.4, "lat": 39.4})

    def test_recalc_switching_to_geo_does_not_reuse_transport_midpoint(self):
        sid = app_v2.session_create({
            "location_a": {"lng": 116.0, "lat": 39.0},
            "location_b": {"lng": 118.0, "lat": 41.0},
            "city": "北京",
            "midpoint": {"lng": 116.2, "lat": 39.2},
            "midpoint_details": {
                "method": "driving_time",
                "midpoint": {"lng": 116.2, "lat": 39.2},
            },
            "midpoint_mode": "transport",
            "search_radius_m": 2000,
            "pois_base": [{
                "route_id": "r0",
                "name": "测试地点",
                "lng": 117.0,
                "lat": 40.0,
                "rating": 4.5,
            }],
        })

        with patch.object(app_v2, "calculate_routes", return_value=[]):
            response = app_v2.app.test_client().post(
                "/api/v2/routes",
                json={
                    "session_id": sid,
                    "midpoint_mode": "geo",
                    "prefer_a": "walking",
                    "prefer_b": "driving",
                },
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["midpoint"], {"lng": 117.0, "lat": 40.0})
        self.assertEqual(payload["midpoint_details"]["method"], "geo")


if __name__ == "__main__":
    unittest.main()
