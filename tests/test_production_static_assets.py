import asyncio
import unittest

import httpx

from app.main import app


class ProductionStaticAssetsTest(unittest.TestCase):
    def test_frontend_script_urls_serve_javascript(self):
        asyncio.run(self._assert_frontend_script_urls_serve_javascript())

    async def _assert_frontend_script_urls_serve_javascript(self):
        expected_markers = {
            "/static/candidate-count.js": "CandidateCount",
            "/static/midpoint-state.js": "MidpointState",
            "/static/map-routes.js": "MapRoutes",
        }

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            for url, marker in expected_markers.items():
                with self.subTest(url=url):
                    response = await client.get(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(
                        "javascript",
                        response.headers["content-type"],
                    )
                    self.assertIn(marker, response.text)


if __name__ == "__main__":
    unittest.main()
