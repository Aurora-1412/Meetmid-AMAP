import unittest
from unittest.mock import patch

import amap_client
from app import amap_client as deployment_amap_client


class RoutePreferenceTest(unittest.TestCase):
    def _assert_preference_behavior(self, module):
        transit = {
            "success": True,
            "mode": "transit",
            "duration_minutes": 12,
        }
        walking = {
            "success": True,
            "mode": "walking",
            "duration_minutes": 5,
        }
        driving = {
            "success": True,
            "mode": "driving",
            "duration_minutes": 8,
        }
        cycling = {
            "success": True,
            "mode": "cycling",
            "duration_minutes": 18,
        }

        with (
            patch.object(
                module, "amap_transit_route", return_value=transit
            ) as transit_call,
            patch.object(
                module, "amap_walking_route", return_value=walking
            ) as walking_call,
            patch.object(
                module, "amap_driving_route", return_value=driving
            ) as driving_call,
            patch.object(
                module, "amap_cycling_route", return_value=cycling
            ) as cycling_call,
        ):
            selected_short_transit = module.amap_get_best_route(
                116.0, 39.0, 116.001, 39.001,
                prefer="transit",
            )
            selected_walking = module.amap_get_best_route(
                116.0, 39.0, 117.0, 40.0,
                prefer="walking",
            )
            selected_driving = module.amap_get_best_route(
                116.0, 39.0, 117.0, 40.0,
                prefer="driving",
            )
            selected_cycling = module.amap_get_best_route(
                116.0, 39.0, 117.0, 40.0,
                prefer="cycling",
            )

        # 交通中点以公交为计算基准，但很近的候选地点仍允许全程步行。
        self.assertEqual(selected_short_transit["mode"], "walking")
        self.assertEqual(selected_walking["mode"], "walking")
        self.assertEqual(selected_driving["mode"], "driving")
        self.assertEqual(selected_cycling["mode"], "cycling")
        transit_call.assert_called_once()
        self.assertEqual(walking_call.call_count, 2)
        driving_call.assert_called_once()
        cycling_call.assert_called_once()

    def test_local_adapter_allows_short_walks_and_honors_explicit_walking(self):
        self._assert_preference_behavior(amap_client)

    def test_deployment_adapter_allows_short_walks_and_explicit_walking(self):
        self._assert_preference_behavior(deployment_amap_client)


if __name__ == "__main__":
    unittest.main()
