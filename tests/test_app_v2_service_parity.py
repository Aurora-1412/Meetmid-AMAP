import unittest
from unittest.mock import patch

import app_v2


class AppV2ServiceParityTest(unittest.TestCase):
    def test_config_reports_whether_deepseek_is_enabled(self):
        client = app_v2.app.test_client()

        with patch.object(app_v2, "DEEPSEEK_API_KEY", "configured-key"):
            enabled = client.get("/api/config")
        with patch.object(app_v2, "DEEPSEEK_API_KEY", ""):
            disabled = client.get("/api/config")

        self.assertTrue(enabled.get_json()["ai_enabled"])
        self.assertFalse(disabled.get_json()["ai_enabled"])

    def test_search_does_not_expose_internal_exception_details(self):
        client = app_v2.app.test_client()
        payload = {
            "location_a": {"lng": 116.0, "lat": 39.0},
            "location_b": {"lng": 117.0, "lat": 40.0},
            "query": "咖啡馆",
        }

        with (
            patch.object(app_v2, "AMAP_KEY", "configured-key"),
            patch.object(app_v2, "DEEPSEEK_API_KEY", "configured-key"),
            patch.object(
                app_v2,
                "run_pipeline",
                side_effect=RuntimeError("secret backend detail"),
            ),
            self.assertLogs(app_v2.app.logger, level="ERROR"),
        ):
            response = client.post("/api/v2/search", json=payload)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json(),
            {"success": False, "error": "服务内部错误，请稍后重试"},
        )

    def test_route_recalculation_does_not_expose_internal_exception_details(self):
        session_id = app_v2.session_create({
            "location_a": {"lng": 116.0, "lat": 39.0},
            "location_b": {"lng": 117.0, "lat": 40.0},
            "pois_base": [{
                "route_id": "r0",
                "name": "测试地点",
                "lng": 116.5,
                "lat": 39.5,
            }],
            "geo_midpoint_details": {
                "method": "geo",
                "midpoint": {"lng": 116.5, "lat": 39.5},
                "suggested_search_radius_m": 3000,
            },
        })

        with patch.object(
            app_v2,
            "calculate_routes",
            side_effect=RuntimeError("secret routing detail"),
        ), self.assertLogs(app_v2.app.logger, level="ERROR"):
            response = app_v2.app.test_client().post(
                "/api/v2/routes",
                json={"session_id": session_id},
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json(),
            {"success": False, "error": "路线计算失败，请稍后重试"},
        )

    def test_route_recalculation_sanitizes_malformed_cached_session(self):
        session_id = app_v2.session_create({
            "location_a": {"lng": 116.0, "lat": 39.0},
            "pois_base": [{
                "route_id": "r0",
                "name": "测试地点",
                "lng": 116.5,
                "lat": 39.5,
            }],
        })

        with self.assertLogs(app_v2.app.logger, level="ERROR"):
            response = app_v2.app.test_client().post(
                "/api/v2/routes",
                json={"session_id": session_id},
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json(),
            {"success": False, "error": "路线计算失败，请稍后重试"},
        )

    def test_nearby_search_does_not_expose_map_request_details(self):
        with patch.object(
            app_v2.requests,
            "get",
            side_effect=RuntimeError("secret map endpoint detail"),
        ), self.assertLogs(app_v2.app.logger, level="ERROR"):
            response = app_v2.app.test_client().post(
                "/api/nearby-search",
                json={
                    "lng": 116.5,
                    "lat": 39.5,
                    "keyword": "地铁站",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "success": False,
                "error": "地图服务请求失败",
                "pois": [],
            },
        )

    def test_nearby_search_does_not_forward_map_api_error_details(self):
        class FailedMapResponse:
            def json(self):
                return {"status": "0", "info": "secret provider detail"}

        with patch.object(
            app_v2.requests,
            "get",
            return_value=FailedMapResponse(),
        ):
            response = app_v2.app.test_client().post(
                "/api/nearby-search",
                json={
                    "lng": 116.5,
                    "lat": 39.5,
                    "keyword": "地铁站",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"success": False, "error": "搜索失败", "pois": []},
        )

    def test_geocode_suggest_does_not_expose_map_request_details(self):
        with patch.object(
            app_v2.requests,
            "get",
            side_effect=RuntimeError("secret suggestion detail"),
        ), self.assertLogs(app_v2.app.logger, level="ERROR"):
            response = app_v2.app.test_client().post(
                "/api/geocode-suggest",
                json={"keyword": "天安门"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"tips": []})

    def test_frontend_script_urls_are_available_from_flask(self):
        expected_markers = {
            "/static/candidate-count.js": "CandidateCount",
            "/static/midpoint-state.js": "MidpointState",
            "/static/map-routes.js": "MapRoutes",
        }
        client = app_v2.app.test_client()

        for url, marker in expected_markers.items():
            with self.subTest(url=url):
                with client.get(url) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("javascript", response.content_type)
                    self.assertIn(marker, response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
