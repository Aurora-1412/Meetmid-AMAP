import unittest
from pathlib import Path
from unittest.mock import patch

import amap_client
import app_v2
from app import amap_client as deployment_amap_client


TRANSIT_PAYLOAD = {
    "status": "1",
    "route": {
        "transits": [{
            "cost": {"duration": "1815"},
            "distance": "12345",
            "segments": [{
                "walking": {"cost": {"duration": "300"}},
                "bus": {"buslines": [{"name": "地铁1号线"}]},
            }],
        }],
    },
}


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class AmapTransitV5Test(unittest.TestCase):
    def _assert_v5_request(self, module):
        with patch.object(
            module.requests, "get",
            return_value=_FakeResponse(TRANSIT_PAYLOAD),
        ) as request:
            route = module.amap_transit_route(
                116.0, 39.0, 116.5, 39.5,
                city="北京", strategy=3,
            )

        url = request.call_args.args[0]
        params = request.call_args.kwargs["params"]
        self.assertEqual(
            url, "https://restapi.amap.com/v5/direction/transit/integrated"
        )
        self.assertEqual(params["city1"], "110000")
        self.assertEqual(params["city2"], "110000")
        self.assertEqual(params["strategy"], 3)
        self.assertEqual(params["AlternativeRoute"], 1)
        self.assertEqual(params["show_fields"], "cost,navi")
        self.assertNotIn("originpoi", params)

        self.assertTrue(route["success"])
        self.assertEqual(route["strategy"], 3)
        self.assertEqual(route["duration_minutes"], 30)
        self.assertEqual(route["distance_km"], 12.3)
        self.assertIn("步行5分钟", route["line_summary"])
        self.assertIn("地铁1号线", route["line_summary"])

    def test_local_adapter_uses_v5_strategy(self):
        self._assert_v5_request(amap_client)

    def test_deployment_adapter_uses_v5_strategy(self):
        self._assert_v5_request(deployment_amap_client)

    def test_subway_map_strategy_requires_poi_ids(self):
        for module in (amap_client, deployment_amap_client):
            with self.subTest(module=module.__name__):
                with patch.object(module.requests, "get") as request:
                    route = module.amap_transit_route(
                        116.0, 39.0, 116.5, 39.5, strategy=6,
                    )
                self.assertFalse(route["success"])
                self.assertIn("POI", route["error"])
                request.assert_not_called()

    def test_subway_map_strategy_sends_poi_ids(self):
        for module in (amap_client, deployment_amap_client):
            with self.subTest(module=module.__name__):
                with patch.object(
                    module.requests, "get",
                    return_value=_FakeResponse(TRANSIT_PAYLOAD),
                ) as request:
                    route = module.amap_transit_route(
                        116.0, 39.0, 116.5, 39.5,
                        strategy=6,
                        origin_poi="B000A8UIN8",
                        destination_poi="B000A7G3Y2",
                    )
                params = request.call_args.kwargs["params"]
                self.assertEqual(params["originpoi"], "B000A8UIN8")
                self.assertEqual(params["destinationpoi"], "B000A7G3Y2")
                self.assertTrue(route["success"])


class TransitStrategyEndpointTest(unittest.TestCase):
    def setUp(self):
        self.client = app_v2.app.test_client()

    def test_search_rejects_out_of_range_strategy_before_pipeline(self):
        with patch.object(app_v2, "run_pipeline") as pipeline:
            response = self.client.post("/api/v2/search", json={
                "location_a": {"lng": 116.0, "lat": 39.0},
                "location_b": {"lng": 117.0, "lat": 40.0},
                "query": "咖啡馆",
                "transit_strategy": 9,
            })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"],
            "公交换乘策略必须是 0 到 8 之间的整数",
        )
        pipeline.assert_not_called()

    def test_search_rejects_subway_map_without_poi_id(self):
        with patch.object(app_v2, "run_pipeline") as pipeline:
            response = self.client.post("/api/v2/search", json={
                "location_a": {"lng": 116.0, "lat": 39.0},
                "location_b": {"lng": 117.0, "lat": 40.0},
                "query": "咖啡馆",
                "prefer_a": "transit",
                "transit_strategy": 6,
            })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"],
            "使用地铁图模式时必须从搜索候选中选择地点",
        )
        pipeline.assert_not_called()

    def test_search_forwards_strategy_to_pipeline(self):
        payload = {
            "location_a": {"lng": 116.0, "lat": 39.0, "poi_id": "B0A"},
            "location_b": {"lng": 117.0, "lat": 40.0, "poi_id": "B0B"},
            "query": "咖啡馆",
            "prefer_a": "transit",
            "transit_strategy": 6,
        }

        with (
            patch.object(app_v2, "AMAP_KEY", "configured-key"),
            patch.object(app_v2, "DEEPSEEK_API_KEY", "configured-key"),
            patch.object(
                app_v2, "run_pipeline", return_value={"success": True},
            ) as pipeline,
        ):
            response = self.client.post("/api/v2/search", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            pipeline.call_args.args[-1], 6,
        )

    def test_recalc_forwards_and_persists_strategy(self):
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

        with patch.object(app_v2, "calculate_routes", return_value=[]) as calc:
            response = self.client.post("/api/v2/routes", json={
                "session_id": session_id,
                "transit_strategy": 2,
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calc.call_args.kwargs["transit_strategy"], 2)
        self.assertEqual(
            app_v2.session_get(session_id)["transit_strategy"], 2
        )

    def test_deployment_entrypoint_mirrors_transit_strategy_contract(self):
        source = Path("app/main.py").read_text(encoding="utf-8")
        for marker in (
            "validate_transit_strategy(",
            "transit_strategy",
            '"poi_id":',
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
