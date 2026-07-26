# Production Static Assets Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the production FastAPI entry serve the same `/static/*` JavaScript URLs that work under the local Flask entry, then publish and runtime-test a ZIP containing `.env`.

**Architecture:** Register a dedicated `/static` FastAPI mount before the existing catch-all `/` mount. Keep the frontend URLs unchanged so local Flask and production FastAPI share one `static/index.html`.

**Tech Stack:** Python 3, FastAPI/Starlette StaticFiles, Flask, `unittest`, Node test runner, Uvicorn, ZIP.

## Global Constraints

- Preserve `app_v2.py` behavior and the existing frontend asset URLs.
- Register `/static` before the catch-all `/` mount.
- Keep the fix limited to production static routing and its regression test.
- Publish `dist/midpoint-recommender.zip` with `.env`, without `.env.example`, tests, virtual environments, caches, or Git data.

---

### Task 1: Production static asset routing regression

**Files:**
- Create: `tests/test_production_static_assets.py`
- Modify: `app/main.py:1093-1097`

**Interfaces:**
- Consumes: `<script src="static/...">` references in `static/index.html`.
- Produces: FastAPI URL mount `/static` backed by the local `static/` directory.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
<temporary-venv>/bin/python -m unittest tests.test_production_static_assets
```

Expected: FAIL because all three URLs return 404 through the root mount.

- [ ] **Step 3: Add the minimal production mount**

Insert immediately before the existing root mount:

```python
app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static-assets",
)
```

- [ ] **Step 4: Run focused and complete regression tests**

Run:

```bash
<temporary-venv>/bin/python -m unittest tests.test_production_static_assets
venv/bin/python -m unittest tests.test_candidate_count tests.test_deepseek_model tests.test_midpoint_logic tests.test_midpoint_selection tests.test_route_cache tests.test_route_geometry tests.test_route_preferences
node --test tests/*.test.js
```

Expected: focused Python test passes; all Python and Node tests pass.

- [ ] **Step 5: Commit production code and regression test**

```bash
git add app/main.py tests/test_production_static_assets.py
git commit -m "fix: serve frontend assets in production"
```

### Task 2: Rebuild and runtime-test the production ZIP

**Files:**
- Replace generated artifact: `dist/midpoint-recommender.zip`

**Interfaces:**
- Consumes: current production source, scripts, README, and `.env`.
- Produces: a deployable ZIP whose `start.sh` launches `app.main:app`.

- [ ] **Step 1: Rebuild the archive from an explicit manifest**

Build a temporary ZIP with exactly these entries, then atomically replace the
delivery artifact:

```text
app/__init__.py
app/ai_client.py
app/amap_client.py
app/config.py
app/main.py
static/candidate-count.js
static/index.html
static/map-routes.js
static/midpoint-state.js
candidate_count.py
requirements.txt
install.sh
start.sh
health.sh
stop.sh
uninstall.sh
README.md
.env
```

- [ ] **Step 2: Verify archive integrity and source identity**

Run `unzip -t`, then compare every archived entry byte-for-byte with the
corresponding workspace file. Confirm `.env` occurs once and `.env.example`,
tests, virtual environments, caches, and Git data occur zero times.

- [ ] **Step 3: Install production dependencies in an isolated temporary venv**

Create a temporary directory under `/private/tmp`, extract the ZIP, create a
virtual environment there, and install `requirements.txt`. Do not modify the
project's existing `venv`.

- [ ] **Step 4: Start the extracted production entry and verify HTTP routes**

Run the extracted package with:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 3100
```

Verify HTTP 200 for:

```text
/
/static/candidate-count.js
/static/midpoint-state.js
/static/map-routes.js
/api/config
/health
```

For `/api/config`, inspect only boolean status fields; do not print key values.

- [ ] **Step 5: Verify the extracted package in a browser**

Open `http://127.0.0.1:3100/`. Confirm `CandidateCount`, `MidpointState`, and
`MapRoutes` load without reference errors, the API badge leaves “检测中...”, and
the map initializes. Check browser error logs are empty, then close the tab and
stop Uvicorn.

- [ ] **Step 6: Report the artifact**

Report the ZIP path, size, SHA-256, test counts, runtime HTTP checks, browser
result, and the warning that the archive contains real secrets.
