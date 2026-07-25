import unittest
from unittest.mock import patch

import app_v2
from candidate_count import parse_candidate_count


class CandidateCountTest(unittest.TestCase):
    def test_deployment_entrypoint_uses_shared_candidate_contract(self):
        source = (
            __import__("pathlib").Path("app/main.py").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn(
            "from candidate_count import parse_candidate_count",
            source,
        )
        self.assertIn(
            'candidate_count = parse_candidate_count(',
            source,
        )
        self.assertIn(
            '"candidate_count": candidate_count',
            source,
        )

    def test_default_and_boundaries(self):
        self.assertEqual(parse_candidate_count(), 10)
        self.assertEqual(parse_candidate_count(1), 1)
        self.assertEqual(parse_candidate_count(20), 20)

    def test_invalid_values_are_rejected(self):
        for value in (0, -1, 21, 3.5, "10", True, False):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "候选点数量必须是 1 到 20 之间的整数",
                ):
                    parse_candidate_count(value)

    def test_flask_endpoint_rejects_invalid_count_before_pipeline(self):
        with patch.object(app_v2, "run_pipeline") as pipeline:
            response = app_v2.app.test_client().post(
                "/api/v2/search",
                json={
                    "location_a": {"lng": 116.0, "lat": 39.0},
                    "location_b": {"lng": 117.0, "lat": 40.0},
                    "query": "咖啡馆",
                    "candidate_count": 21,
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"],
            "候选点数量必须是 1 到 20 之间的整数",
        )
        pipeline.assert_not_called()

    def test_user_count_overrides_llm_top_n(self):
        twenty_pois = [
            {
                "name": f"地点{i}",
                "lng": 116.0 + i / 1000,
                "lat": 39.0,
                "rating": 4.5,
            }
            for i in range(20)
        ]
        calculated_sizes = []

        def fake_search(_a, _b, _plan, _city, search_ctx, *_args):
            search_ctx["pois"] = twenty_pois
            return {
                "success": True,
                "midpoint": {"lng": 116.5, "lat": 39.5},
                "midpoint_details": {"method": "geo"},
                "search_radius_m": 3000,
            }

        def fake_calculate(pois, *_args, **_kwargs):
            calculated_sizes.append(len(pois))
            return pois

        with (
            patch.object(
                app_v2,
                "agent_plan",
                return_value={
                    "top_n": 20,
                    "min_rating": 4.0,
                    "sort_weights": {},
                },
            ),
            patch.object(app_v2, "agent_search", side_effect=fake_search),
            patch.object(
                app_v2, "calculate_routes", side_effect=fake_calculate
            ),
            patch.object(app_v2, "agent_summarize", return_value="完成"),
        ):
            result = app_v2.run_pipeline(
                "咖啡馆",
                {"lng": 116.0, "lat": 39.0},
                {"lng": 117.0, "lat": 40.0},
                candidate_count=3,
            )

        self.assertEqual(calculated_sizes, [3])
        self.assertEqual(result["candidate_count"], 3)
        self.assertEqual(result["plan"]["top_n"], 3)
        self.assertEqual(len(result["pois"]), 3)


if __name__ == "__main__":
    unittest.main()
