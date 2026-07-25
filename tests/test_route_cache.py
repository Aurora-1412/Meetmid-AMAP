import unittest
from unittest.mock import patch

import app_v2


class RouteCacheTest(unittest.TestCase):
    def setUp(self):
        app_v2._sessions.clear()

    def test_calculate_routes_caches_geometry_but_omits_it_from_poi_response(self):
        geometry = {
            "mode": "driving",
            "segments": [{
                "mode": "driving",
                "path": [[116.0, 39.0], [116.5, 39.5]],
            }],
        }
        route = {
            "success": True,
            "mode": "driving",
            "duration_minutes": 20,
            "duration_text": "20分钟",
            "distance_text": "10公里",
            "line_summary": "",
            "all_modes": {},
            "geometry": geometry,
        }
        cache = {}

        with (
            patch.object(app_v2, "amap_get_best_route", return_value=route),
            patch.object(app_v2.time, "sleep"),
        ):
            pois = app_v2.calculate_routes(
                [{
                    "route_id": "r0",
                    "name": "测试地点",
                    "lng": 116.5,
                    "lat": 39.5,
                    "rating": 4.5,
                }],
                {"lng": 116.0, "lat": 39.0},
                {"lng": 117.0, "lat": 40.0},
                prefer_a="driving",
                prefer_b="driving",
                geometry_cache=cache,
            )

        self.assertNotIn("geometry", pois[0]["transport_from_a"])
        self.assertNotIn("geometry", pois[0]["transport_from_b"])
        self.assertEqual(cache["r0"]["route_a"], geometry)
        self.assertEqual(cache["r0"]["route_b"], geometry)

    def test_cached_route_endpoint_returns_only_the_requested_poi(self):
        sid = app_v2.session_create({
            "route_geometries": {
                "r0": {
                    "route_a": {"mode": "walking", "segments": []},
                    "route_b": None,
                    "errors": {"b": "公交路线规划失败"},
                },
                "r1": {
                    "route_a": {"mode": "driving", "segments": []},
                    "route_b": {"mode": "driving", "segments": []},
                    "errors": {},
                },
            }
        })

        response = app_v2.app.test_client().get(
            f"/api/v2/session/{sid}/routes/r0"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "success": True,
                "route_id": "r0",
                "route_a": {"mode": "walking", "segments": []},
                "route_b": None,
                "errors": {"b": "公交路线规划失败"},
            },
        )

    def test_cached_route_endpoint_rejects_unknown_session_or_route(self):
        client = app_v2.app.test_client()
        self.assertEqual(
            client.get("/api/v2/session/missing/routes/r0").status_code,
            404,
        )

        sid = app_v2.session_create({"route_geometries": {}})
        self.assertEqual(
            client.get(f"/api/v2/session/{sid}/routes/missing").status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
