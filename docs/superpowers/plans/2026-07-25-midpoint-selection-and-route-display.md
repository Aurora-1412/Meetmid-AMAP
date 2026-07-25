# Midpoint Selection and Route Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users choose a geographic or transport-time midpoint and display cached blue/orange A/B routes when a recommendation is selected.

**Architecture:** Implement and verify the feature first in the local Flask application (`app_v2.py`, `amap_client.py`, and the shared static frontend). Route calls will normalize only the selected route geometry and save it in the existing one-hour Session under a stable `route_id`; the initial response remains small and a read-only endpoint returns one POI’s cached paths. After local tests and browser verification pass, port the verified policy and geometry code to the FastAPI/Runway deployment package without changing its AI configuration.

**Tech Stack:** Python 3.10, Flask, unittest, AMap Web Service API, AMap JS API 2.0, Node.js built-in `node:test`, and FastAPI for the deployment adapter.

## Global Constraints

- The local source of truth and test entrypoint is `app_v2.py`; run it directly with `venv/bin/python app_v2.py`.
- Default `midpoint_mode` is exactly `"geo"` for backward compatibility.
- `"transport"` accepts only `prefer_a == prefer_b == "transit"` or `prefer_a == prefer_b == "driving"`.
- Geographic mode preserves independent A/B transport choices.
- The initial search and route-recalculation responses must not contain geometry arrays.
- A selected POI’s cached A route is blue (`#2196F3`) and B route is orange (`#FF7A45`).
- Selecting another POI, searching, recalculating, or clearing a location removes old route overlays.
- Do not add DeepSeek configuration to `app/`; it must retain Runway `AI_BASE_URL`/`AI_API_KEY` configuration.
- Preserve all pre-existing uncommitted changes. The target files are already dirty, so implementation checkpoints use focused diffs instead of commits; do not stage or commit those files without separate user authorization.

---

### Task 1: Explicit midpoint policy in the local Flask backend

**Files:**
- Create: `tests/test_midpoint_selection.py`
- Modify: `app_v2.py:27-36`
- Modify: `app_v2.py:272-375`
- Modify: `app_v2.py:568-642`
- Modify: `app_v2.py:661-775`

**Interfaces:**
- Produces: `validate_midpoint_choice(midpoint_mode: str, prefer_a: str, prefer_b: str) -> str | None`
- Produces: `compute_requested_midpoint(lng1: float, lat1: float, lng2: float, lat2: float, *, midpoint_mode: str, prefer_a: str, prefer_b: str, city: str, departure_time: str | None) -> dict`
- Extends: `run_pipeline(user_query: str, location_a: dict, location_b: dict, city: str = "北京", prefer_a: str = "auto", prefer_b: str = "auto", departure_time: str | None = None, midpoint_mode: str = "geo") -> dict`
- Extends: search and recalc payloads with `midpoint_mode`

- [ ] **Step 1: Write failing policy and endpoint-validation tests**

```python
# tests/test_midpoint_selection.py
import unittest
from unittest.mock import patch

import app_v2


class MidpointSelectionTest(unittest.TestCase):
    def test_geo_mode_keeps_independent_transport_preferences(self):
        error = app_v2.validate_midpoint_choice("geo", "walking", "driving")
        self.assertIsNone(error)

        result = app_v2.compute_requested_midpoint(
            116.0, 39.0, 118.0, 41.0,
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
                116.0, 39.0, 117.0, 40.0,
                midpoint_mode="transport",
                prefer_a="transit",
                prefer_b="transit",
                city="北京",
                departure_time="09:30",
            )
            driving = app_v2.compute_requested_midpoint(
                116.0, 39.0, 117.0, 40.0,
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
venv/bin/python -m unittest tests.test_midpoint_selection -v
```

Expected: failure because `validate_midpoint_choice` and
`compute_requested_midpoint` do not exist.

- [ ] **Step 3: Implement the midpoint policy and pass it through the pipeline**

Add imports from `amap_client`:

```python
from amap_client import (
    DEEPSEEK_API_KEY,
    AMAP_KEY,
    AMAP_JS_KEY,
    amap_geocode,
    amap_get_best_route,
    amap_search_nearby,
    haversine_distance,
    find_balanced_midpoint,
    find_time_midpoint,
)
```

Add policy functions near the common helpers:

```python
def validate_midpoint_choice(
    midpoint_mode: str, prefer_a: str, prefer_b: str
) -> str | None:
    if midpoint_mode not in ("geo", "transport"):
        return "中点方式只支持位置中点或交通中点"
    if midpoint_mode == "transport":
        valid = (
            prefer_a == prefer_b
            and prefer_a in ("transit", "driving")
        )
        if not valid:
            return "选择交通中点时，双方必须同时选择公交地铁或同时选择驾车"
    return None


def compute_requested_midpoint(
    lng1: float, lat1: float,
    lng2: float, lat2: float,
    *,
    midpoint_mode: str,
    prefer_a: str,
    prefer_b: str,
    city: str,
    departure_time: str | None,
) -> dict:
    error = validate_midpoint_choice(midpoint_mode, prefer_a, prefer_b)
    if error:
        raise ValueError(error)
    if midpoint_mode == "geo":
        result = find_balanced_midpoint(lng1, lat1, lng2, lat2)
        result["method"] = "geo"
        return result
    return find_time_midpoint(
        lng1, lat1, lng2, lat2,
        city=city,
        departure_time=departure_time,
        mode=prefer_a,
    )
```

Pass `midpoint_mode` into `agent_search`, save it in `search_ctx`, and replace
the adaptive call in `_exec_search_tool`:

```python
result = compute_requested_midpoint(
    args["lng1"], args["lat1"],
    args["lng2"], args["lat2"],
    midpoint_mode=search_ctx.get("midpoint_mode", "geo"),
    prefer_a=search_ctx.get("prefer_a", "auto"),
    prefer_b=search_ctx.get("prefer_b", "auto"),
    city=search_ctx.get("city", "北京"),
    departure_time=search_ctx.get("departure_time"),
)
search_ctx["last_midpoint_details"] = result
```

Make `agent_search` return both the coordinate and metadata:

```python
return {
    "success": found_count > 0,
    "midpoint": midpoint,
    "midpoint_details": search_ctx.get("last_midpoint_details", {}),
    "search_radius_m": radius,
}
```

Read and validate `midpoint_mode` in both Flask endpoints before any LLM or
route work. Store it in Session and return `midpoint_details` from search and
recalc. In recalc, call `compute_requested_midpoint` only for transport mode;
geo mode reuses the Session midpoint.

- [ ] **Step 4: Run policy tests and the existing midpoint suite**

Run:

```bash
venv/bin/python -m unittest \
  tests.test_midpoint_selection \
  tests.test_midpoint_logic -v
```

Expected: all tests pass; existing adaptive-midpoint helpers remain intact.

- [ ] **Step 5: Review the focused diff checkpoint**

Run:

```bash
git diff -- app_v2.py tests/test_midpoint_selection.py
```

Confirm no `app/` file or unrelated ranking change is included. Do not commit
because `app_v2.py` already contains pre-existing user changes.

---

### Task 2: Normalize route geometry during existing AMap calls

**Files:**
- Create: `tests/test_route_geometry.py`
- Modify: `amap_client.py:110-340`

**Interfaces:**
- Produces: `parse_polyline(polyline: str) -> list[list[float]]`
- Produces: `build_step_geometry(steps: list, mode: str) -> list[dict]`
- Produces: `build_transit_geometry(transit: dict) -> dict | None`
- Extends successful route dictionaries with
  `geometry: {"mode": str, "segments": list[dict]}`

- [ ] **Step 1: Write failing geometry tests with complete AMap-shaped fixtures**

```python
# tests/test_route_geometry.py
import unittest

import amap_client


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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run geometry tests and verify RED**

Run:

```bash
venv/bin/python -m unittest tests.test_route_geometry -v
```

Expected: failure because the four geometry helpers do not exist.

- [ ] **Step 3: Implement geometry normalization helpers**

```python
def parse_polyline(polyline: str) -> list[list[float]]:
    points: list[list[float]] = []
    for item in (polyline or "").split(";"):
        parts = item.split(",")
        if len(parts) != 2:
            continue
        try:
            point = [float(parts[0]), float(parts[1])]
        except (TypeError, ValueError):
            continue
        if not points or points[-1] != point:
            points.append(point)
    return points


def build_step_geometry(steps: list, mode: str) -> list[dict]:
    segments = []
    for step in steps or []:
        path = parse_polyline(step.get("polyline", ""))
        if len(path) >= 2:
            segments.append({"mode": mode, "path": path})
    return segments


def build_route_geometry(mode: str, segments: list[dict]) -> dict | None:
    valid = [
        segment for segment in segments
        if len(segment.get("path") or []) >= 2
    ]
    return {"mode": mode, "segments": valid} if valid else None


def build_transit_geometry(transit: dict) -> dict | None:
    segments: list[dict] = []
    for segment in transit.get("segments", []):
        walking = segment.get("walking") or {}
        segments.extend(build_step_geometry(walking.get("steps", []), "walking"))
        for busline in (segment.get("bus") or {}).get("buslines", []):
            path = parse_polyline(busline.get("polyline", ""))
            if len(path) >= 2:
                segments.append({"mode": "transit", "path": path})
    return build_route_geometry("transit", segments)
```

- [ ] **Step 4: Attach geometry to each successful route result**

Request detailed driving/walking data with `extensions: "all"`. Add:

```python
"geometry": build_route_geometry(
    "driving",
    build_step_geometry(path.get("steps", []), "driving"),
),
```

to driving, the corresponding `"walking"` expression to walking, and
`build_transit_geometry(transit)` to transit. For cycling, normalize
`path.get("steps", [])` using mode `"cycling"`.

Keep `all_modes` summaries geometry-free. The chosen `best` route may retain
its top-level `geometry`, because the Flask formatter strips it before public
responses.

- [ ] **Step 5: Run geometry and existing tests**

Run:

```bash
venv/bin/python -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 6: Review the focused diff checkpoint**

Run:

```bash
git diff -- amap_client.py tests/test_route_geometry.py
```

Confirm raw AMap responses are not stored and no route call was duplicated.
Do not commit the dirty existing file.

---

### Task 3: Cache per-POI route geometry and expose a read-only Flask endpoint

**Files:**
- Create: `tests/test_route_cache.py`
- Modify: `app_v2.py:116-130`
- Modify: `app_v2.py:408-485`
- Modify: `app_v2.py:568-642`
- Modify: `app_v2.py:697-775`

**Interfaces:**
- Extends: `calculate_routes(pois: list, location_a: dict, location_b: dict, prefer_a: str = "auto", prefer_b: str = "auto", city: str = "北京", departure_time: str | None = None, sort_weights: dict | None = None, geometry_cache: dict | None = None) -> list`
- Produces Session field:
  `route_geometries[route_id] = {"route_a": dict | None, "route_b": dict | None, "errors": dict[str, str]}`
- Produces: `GET /api/v2/session/<session_id>/routes/<route_id>`

- [ ] **Step 1: Write failing cache and endpoint tests**

```python
# tests/test_route_cache.py
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
```

- [ ] **Step 2: Run cache tests and verify RED**

Run:

```bash
venv/bin/python -m unittest tests.test_route_cache -v
```

Expected: `calculate_routes` rejects `geometry_cache`, and the endpoint is 404.

- [ ] **Step 3: Collect geometry without exposing it in formatted routes**

Extend the signature:

```python
def calculate_routes(
    pois: list,
    location_a: dict,
    location_b: dict,
    prefer_a: str = "auto",
    prefer_b: str = "auto",
    city: str = "北京",
    departure_time: str | None = None,
    sort_weights: dict | None = None,
    geometry_cache: dict | None = None,
) -> list:
```

After both raw routes are calculated:

```python
route_id = p.get("route_id")
if geometry_cache is not None and route_id:
    errors = {}
    if not route_a.get("geometry"):
        errors["a"] = route_a.get("error", "未获得路线轨迹")
    if not route_b.get("geometry"):
        errors["b"] = route_b.get("error", "未获得路线轨迹")
    geometry_cache[route_id] = {
        "route_a": route_a.get("geometry"),
        "route_b": route_b.get("geometry"),
        "errors": errors,
    }
```

Do not add geometry to `_format_route`.

- [ ] **Step 4: Assign stable IDs and save cache in search and recalc Sessions**

Immediately after POI filtering:

```python
top_pois = [
    {**poi, "route_id": f"r{index}"}
    for index, poi in enumerate(top_pois)
]
route_geometries: dict = {}
enriched = calculate_routes(
    top_pois,
    location_a,
    location_b,
    prefer_a,
    prefer_b,
    city,
    departure_time,
    sort_weights=plan.get("sort_weights"),
    geometry_cache=route_geometries,
)
```

Store `route_geometries` and `midpoint_mode` in `session_create`. On recalc,
build a fresh dictionary and replace the Session field only after
`calculate_routes` succeeds.

- [ ] **Step 5: Add the read-only endpoint**

```python
@app.route(
    "/api/v2/session/<session_id>/routes/<route_id>",
    methods=["GET"],
)
def api_v2_cached_routes(session_id: str, route_id: str):
    session = session_get(session_id)
    if not session:
        return jsonify({
            "success": False,
            "error": "会话已过期，请重新搜索",
        }), 404
    cached = session.get("route_geometries", {}).get(route_id)
    if cached is None:
        return jsonify({
            "success": False,
            "error": "未找到该地点的路线缓存",
        }), 404
    return jsonify({
        "success": True,
        "route_id": route_id,
        **cached,
    })
```

- [ ] **Step 6: Run backend tests**

Run:

```bash
venv/bin/python -m unittest discover -s tests -v
```

Expected: all midpoint, geometry, and cache tests pass.

- [ ] **Step 7: Review the focused diff checkpoint**

Run:

```bash
git diff -- app_v2.py tests/test_route_cache.py
```

Confirm `route_geometries` is absent from search/recalc JSON responses and is
present only in Session plus the single-route endpoint.

---

### Task 4: Testable frontend state for midpoint selection

**Files:**
- Create: `static/midpoint-state.js`
- Create: `tests/frontend_midpoint_state.test.js`
- Modify: `static/index.html:780-1015`
- Modify: `static/index.html:1069-1090`
- Modify: `static/index.html:1321-1405`
- Modify: `static/index.html:1572-1625`

**Interfaces:**
- Produces: `MidpointState.create()`
- Produces snapshot:
  `{midpointMode, preferA, preferB, geoPreferA, geoPreferB, transportPrefer}`

- [ ] **Step 1: Write failing Node state tests**

```javascript
// tests/frontend_midpoint_state.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const { create } = require('../static/midpoint-state.js');

test('geographic mode preserves independent A and B choices', () => {
  const state = create();
  state.setGeoPreference('A', 'walking');
  state.setGeoPreference('B', 'driving');

  assert.deepEqual(state.snapshot(), {
    midpointMode: 'geo',
    preferA: 'walking',
    preferB: 'driving',
    geoPreferA: 'walking',
    geoPreferB: 'driving',
    transportPrefer: 'transit',
  });
});

test('transport mode synchronizes both sides and restores geo choices', () => {
  const state = create();
  state.setGeoPreference('A', 'cycling');
  state.setGeoPreference('B', 'walking');
  state.setMode('transport');
  state.setTransportPreference('driving');

  assert.deepEqual(state.requestPayload(), {
    midpoint_mode: 'transport',
    prefer_a: 'driving',
    prefer_b: 'driving',
  });

  state.setMode('geo');
  assert.deepEqual(state.requestPayload(), {
    midpoint_mode: 'geo',
    prefer_a: 'cycling',
    prefer_b: 'walking',
  });
});

test('invalid transport preference is rejected', () => {
  const state = create();
  assert.throws(
    () => state.setTransportPreference('auto'),
    /公交地铁或驾车/,
  );
});
```

- [ ] **Step 2: Run Node tests and verify RED**

Run:

```bash
node --test tests/frontend_midpoint_state.test.js
```

Expected: module-not-found failure.

- [ ] **Step 3: Implement the pure midpoint state module**

```javascript
// static/midpoint-state.js
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.MidpointState = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  function create() {
    let midpointMode = 'geo';
    let geoPreferA = 'auto';
    let geoPreferB = 'auto';
    let transportPrefer = 'transit';

    function setMode(mode) {
      if (!['geo', 'transport'].includes(mode)) {
        throw new Error('中点方式只支持位置中点或交通中点');
      }
      midpointMode = mode;
    }

    function setGeoPreference(which, mode) {
      if (which === 'A') geoPreferA = mode;
      else if (which === 'B') geoPreferB = mode;
      else throw new Error('地点只支持 A 或 B');
    }

    function setTransportPreference(mode) {
      if (!['transit', 'driving'].includes(mode)) {
        throw new Error('交通中点只支持公交地铁或驾车');
      }
      transportPrefer = mode;
    }

    function requestPayload() {
      const shared = midpointMode === 'transport';
      return {
        midpoint_mode: midpointMode,
        prefer_a: shared ? transportPrefer : geoPreferA,
        prefer_b: shared ? transportPrefer : geoPreferB,
      };
    }

    function snapshot() {
      const payload = requestPayload();
      return {
        midpointMode,
        preferA: payload.prefer_a,
        preferB: payload.prefer_b,
        geoPreferA,
        geoPreferB,
        transportPrefer,
      };
    }

    return {
      setMode,
      setGeoPreference,
      setTransportPreference,
      requestPayload,
      snapshot,
    };
  }

  return { create };
});
```

- [ ] **Step 4: Add and synchronize the UI controls**

Load `midpoint-state.js` before the existing inline application script. Add
two midpoint buttons to the main form and recalc panel, wrap the existing A/B
preference groups in an independent-mode container, and add a hidden shared
container containing only:

```html
<button class="pref-btn active" data-mode="transit">🚇 双方公交地铁</button>
<button class="pref-btn" data-mode="driving">🚗 双方驾车</button>
```

Create one `midpointState` instance. Replace direct preference mutation with
state methods, then call `syncMidpointControls()` to:

- synchronize both midpoint button groups;
- show independent preferences only for `geo`;
- show shared transit/driving only for `transport`;
- update existing `preferA` and `preferB` compatibility globals from
  `midpointState.snapshot()`.

Merge `midpointState.requestPayload()` into both search and recalc request
bodies.

- [ ] **Step 5: Run state and backend tests**

Run:

```bash
node --test tests/frontend_midpoint_state.test.js
venv/bin/python -m unittest discover -s tests -v
```

Expected: all pass.

- [ ] **Step 6: Review the frontend state checkpoint**

Run:

```bash
git diff -- static/index.html static/midpoint-state.js \
  tests/frontend_midpoint_state.test.js
```

Confirm transport mode has no “fastest”, walking, or cycling button.

---

### Task 5: Fetch and draw cached blue/orange paths on POI selection

**Files:**
- Create: `static/map-routes.js`
- Create: `tests/frontend_map_routes.test.js`
- Modify: `static/index.html:1069-1090`
- Modify: `static/index.html:1367-1405`
- Modify: `static/index.html:1446-1466`
- Modify: `static/index.html:1572-1655`
- Modify: `static/index.html:1733-1950`

**Interfaces:**
- Produces: `MapRoutes.buildDrawableRoutes(payload) -> list`
- Produces: `MapRoutes.createRequestGate()`
- Produces frontend functions:
  `clearDisplayedRoutes()` and `showRoutesForPoi(poi)`

- [ ] **Step 1: Write failing route-display helper tests**

```javascript
// tests/frontend_map_routes.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const {
  buildDrawableRoutes,
  createRequestGate,
} = require('../static/map-routes.js');

test('A is blue, B is orange, and invalid segments are dropped', () => {
  const drawable = buildDrawableRoutes({
    route_a: {
      mode: 'driving',
      segments: [
        { mode: 'driving', path: [[116.0, 39.0], [116.2, 39.2]] },
        { mode: 'driving', path: [[116.2, 39.2]] },
      ],
    },
    route_b: {
      mode: 'transit',
      segments: [
        { mode: 'walking', path: [[117.0, 40.0], [116.2, 39.2]] },
      ],
    },
  });

  assert.deepEqual(drawable, [
    {
      side: 'A',
      color: '#2196F3',
      mode: 'driving',
      path: [[116.0, 39.0], [116.2, 39.2]],
    },
    {
      side: 'B',
      color: '#FF7A45',
      mode: 'walking',
      path: [[117.0, 40.0], [116.2, 39.2]],
    },
  ]);
});

test('request gate rejects a response from an older POI click', () => {
  const gate = createRequestGate();
  const first = gate.next();
  const second = gate.next();

  assert.equal(gate.isCurrent(first), false);
  assert.equal(gate.isCurrent(second), true);
});
```

- [ ] **Step 2: Run the helper tests and verify RED**

Run:

```bash
node --test tests/frontend_map_routes.test.js
```

Expected: module-not-found failure.

- [ ] **Step 3: Implement the route-display helper module**

```javascript
// static/map-routes.js
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.MapRoutes = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  const COLORS = { A: '#2196F3', B: '#FF7A45' };

  function buildDrawableRoutes(payload) {
    const drawable = [];
    [['A', payload.route_a], ['B', payload.route_b]].forEach(([side, route]) => {
      if (!route) return;
      (route.segments || []).forEach((segment) => {
        if (!Array.isArray(segment.path) || segment.path.length < 2) return;
        drawable.push({
          side,
          color: COLORS[side],
          mode: segment.mode || route.mode,
          path: segment.path,
        });
      });
    });
    return drawable;
  }

  function createRequestGate() {
    let current = 0;
    return {
      next() { current += 1; return current; },
      invalidate() { current += 1; },
      isCurrent(token) { return token === current; },
    };
  }

  return { buildDrawableRoutes, createRequestGate };
});
```

- [ ] **Step 4: Integrate cached route fetching and AMap.Polyline drawing**

Load `map-routes.js`, then add:

```javascript
let routeOverlays = [];
const routeRequestGate = MapRoutes.createRequestGate();

function clearDisplayedRoutes() {
  routeRequestGate.invalidate();
  routeOverlays.forEach((overlay) => map && map.remove(overlay));
  routeOverlays = [];
  const legend = document.getElementById('routeLegend');
  if (legend) legend.style.display = 'none';
}

async function showRoutesForPoi(poi) {
  clearDisplayedRoutes();
  if (!currentSessionId || !poi.route_id || !map) return;
  const token = routeRequestGate.next();
  setRouteStatus('正在读取 A、B 路径…');

  try {
    const response = await fetch(
      `api/v2/session/${encodeURIComponent(currentSessionId)}` +
      `/routes/${encodeURIComponent(poi.route_id)}`
    );
    const payload = await response.json();
    if (!routeRequestGate.isCurrent(token)) return;
    if (!response.ok || !payload.success) {
      throw new Error(payload.error || '路线读取失败');
    }

    MapRoutes.buildDrawableRoutes(payload).forEach((segment) => {
      const overlay = new AMap.Polyline({
        path: segment.path,
        strokeColor: segment.color,
        strokeWeight: 6,
        strokeOpacity: 0.9,
        lineJoin: 'round',
        lineCap: 'round',
        zIndex: 120,
      });
      map.add(overlay);
      routeOverlays.push(overlay);
    });
    showRouteLegend(payload.errors || {});
    if (routeOverlays.length) {
      map.setFitView(routeOverlays, false, [60, 60, 60, 60]);
    }
  } catch (error) {
    if (routeRequestGate.isCurrent(token)) {
      setRouteStatus(error.message, true);
    }
  }
}
```

Make `highlightRestaurant(idx, r)` call `showRoutesForPoi(r)` after the
highlight changes. Because map-marker clicks already call
`highlightRestaurant`, both entrypoints share the same behavior.

Add a compact legend/status element over the map showing blue A and orange B.
Show a single-side error without hiding the successful side.

Call `clearDisplayedRoutes()` when:

- `doSearch()` begins;
- `recalcRoutes()` begins;
- `clearLocation()` runs;
- `renderPoisByCount()` removes the currently selected POI.

After search or recalc, inspect `midpoint_details.fallback_reason` and show it
through the existing non-blocking toast.

- [ ] **Step 5: Run frontend and backend tests**

Run:

```bash
node --test tests/frontend_midpoint_state.test.js \
  tests/frontend_map_routes.test.js
venv/bin/python -m unittest discover -s tests -v
```

Expected: all pass.

- [ ] **Step 6: Review the path-display checkpoint**

Run:

```bash
git diff -- static/index.html static/map-routes.js \
  tests/frontend_map_routes.test.js
```

Confirm no AMap route-planning plugin or new live route request was added;
the frontend reads only the Flask Session endpoint.

---

### Task 6: Local Flask integration and browser verification

**Files:**
- Modify only if a failing verification exposes a scoped defect in:
  `app_v2.py`, `amap_client.py`, `static/index.html`,
  `static/midpoint-state.js`, or `static/map-routes.js`

**Interfaces:**
- Verifies the complete local Flask behavior before any `app/` sync.

- [ ] **Step 1: Run all automated tests and syntax checks**

Run:

```bash
venv/bin/python -m unittest discover -s tests -v
node --test tests/frontend_midpoint_state.test.js \
  tests/frontend_map_routes.test.js
venv/bin/python -m py_compile app_v2.py amap_client.py
```

Expected: all tests pass and compilation exits 0.

- [ ] **Step 2: Start only the local Flask entrypoint**

Run:

```bash
venv/bin/python app_v2.py
```

Expected: Flask listens on `127.0.0.1:5000`. Do not use the dirty working-tree
`start.sh`, because it currently points to the deployment adapter.

- [ ] **Step 3: Use the browser-control skill for visual verification**

Before browser actions, read
`browser:control-in-app-browser/SKILL.md`. Verify:

1. Default mode is geographic and A/B preferences remain independent.
2. Transport mode exposes only shared transit and shared driving.
3. Switching back to geographic restores the earlier independent choices.
4. A real search response contains `route_id` but no `segments`.
5. Clicking a result card draws a blue A path and orange B path.
6. Clicking a map marker triggers the same result.
7. Clicking a second POI clears the first pair of paths.
8. Recalculation and a new search clear old paths.
9. A transport-midpoint fallback displays a toast without aborting search.

- [ ] **Step 4: Inspect browser and server errors**

Confirm the browser console has no JavaScript exception and the Flask terminal
has no unhandled traceback. If a failure occurs, return to the relevant task’s
RED-GREEN cycle rather than patching multiple layers at once.

- [ ] **Step 5: Record the local verification checkpoint**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors. List exactly which local files changed and
keep the pre-existing unrelated files untouched.

---

### Task 7: Port the verified behavior to the FastAPI/Runway deployment package

**Files:**
- Modify: `app/amap_client.py:123-340`
- Modify: `app/amap_client.py:396-430`
- Modify: `app/main.py:22-35`
- Modify: `app/main.py:263-380`
- Modify: `app/main.py:392-470`
- Modify: `app/main.py:535-595`
- Modify: `app/main.py:640-757`
- Modify: `tests/test_route_geometry.py`

**Interfaces:**
- FastAPI accepts and returns the same `midpoint_mode`, `midpoint_details`,
  `route_id`, and cached-route schema as Flask.
- Produces:
  `GET /api/v2/session/{session_id}/routes/{route_id}`
- Retains `app.ai_client` and `app.config` unchanged.

- [ ] **Step 1: Extend pure geometry tests to both map clients and verify RED**

Refactor `tests/test_route_geometry.py` to run the same literal helper cases
against both modules:

```python
import app.amap_client as deployment_amap_client


class DeploymentRouteGeometryParityTest(unittest.TestCase):
    def test_deployment_client_normalizes_the_same_polyline(self):
        self.assertEqual(
            deployment_amap_client.parse_polyline(
                "116.1,39.9;116.1,39.9;116.2,40.0"
            ),
            [[116.1, 39.9], [116.2, 40.0]],
        )

    def test_deployment_client_builds_transit_geometry(self):
        transit = {
            "duration": "600",
            "segments": [{
                "walking": {
                    "steps": [{
                        "polyline": "116.0,39.0;116.1,39.1"
                    }]
                },
                "bus": {"buslines": []},
            }],
        }
        self.assertEqual(
            deployment_amap_client.build_transit_geometry(transit),
            {
                "mode": "transit",
                "segments": [{
                    "mode": "walking",
                    "path": [[116.0, 39.0], [116.1, 39.1]],
                }],
            },
        )
```

Run:

```bash
venv/bin/python -m unittest tests.test_route_geometry -v
```

Expected: deployment module lacks the new helpers.

- [ ] **Step 2: Port the verified pure map functions**

Add these implementations to `app/amap_client.py`:

```python
def parse_polyline(polyline: str) -> list[list[float]]:
    points: list[list[float]] = []
    for item in (polyline or "").split(";"):
        parts = item.split(",")
        if len(parts) != 2:
            continue
        try:
            point = [float(parts[0]), float(parts[1])]
        except (TypeError, ValueError):
            continue
        if not points or points[-1] != point:
            points.append(point)
    return points


def build_step_geometry(steps: list, mode: str) -> list[dict]:
    segments = []
    for step in steps or []:
        path = parse_polyline(step.get("polyline", ""))
        if len(path) >= 2:
            segments.append({"mode": mode, "path": path})
    return segments


def build_route_geometry(mode: str, segments: list[dict]) -> dict | None:
    valid = [
        segment for segment in segments
        if len(segment.get("path") or []) >= 2
    ]
    return {"mode": mode, "segments": valid} if valid else None


def build_transit_geometry(transit: dict) -> dict | None:
    segments: list[dict] = []
    for segment in transit.get("segments", []):
        walking = segment.get("walking") or {}
        segments.extend(build_step_geometry(walking.get("steps", []), "walking"))
        for busline in (segment.get("bus") or {}).get("buslines", []):
            path = parse_polyline(busline.get("polyline", ""))
            if len(path) >= 2:
                segments.append({"mode": "transit", "path": path})
    return build_route_geometry("transit", segments)
```

Attach geometry to successful driving, transit, walking, and cycling results.
Use the deployment client’s existing `_amap_get`; do not replace it with
direct `requests.get`.

Port or retain the verified
`find_time_midpoint(lng1: float, lat1: float, lng2: float, lat2: float, city: str = "北京", departure_time: str | None = None, mode: str = "driving") -> dict`
behavior while also using `_amap_get` for deployment requests.

- [ ] **Step 3: Port the backend policy and cache contract to FastAPI**

Add these synchronous pure functions to `app/main.py`:

```python
def validate_midpoint_choice(
    midpoint_mode: str, prefer_a: str, prefer_b: str
) -> str | None:
    if midpoint_mode not in ("geo", "transport"):
        return "中点方式只支持位置中点或交通中点"
    if midpoint_mode == "transport":
        valid = (
            prefer_a == prefer_b
            and prefer_a in ("transit", "driving")
        )
        if not valid:
            return "选择交通中点时，双方必须同时选择公交地铁或同时选择驾车"
    return None


def compute_requested_midpoint(
    lng1: float,
    lat1: float,
    lng2: float,
    lat2: float,
    *,
    midpoint_mode: str,
    prefer_a: str,
    prefer_b: str,
    city: str,
    departure_time: str | None,
) -> dict:
    error = validate_midpoint_choice(midpoint_mode, prefer_a, prefer_b)
    if error:
        raise ValueError(error)
    if midpoint_mode == "geo":
        result = find_balanced_midpoint(lng1, lat1, lng2, lat2)
        result["method"] = "geo"
        return result
    return find_time_midpoint(
        lng1, lat1, lng2, lat2,
        city=city,
        departure_time=departure_time,
        mode=prefer_a,
    )
```

In both request handlers, read
`midpoint_mode = data.get("midpoint_mode", "geo")`, call
`validate_midpoint_choice`, and return `JSONResponse({"success": False,
"error": error}, status_code=400)` when it returns a string. Add
`midpoint_mode` as the final argument to `run_pipeline` and `agent_search`;
store it in `search_ctx` and Session. Use `compute_requested_midpoint` inside
the `find_midpoint` tool branch. In recalc, reuse the Session coordinate for
`geo`; for `transport`, compute a fresh midpoint using the current shared
preference.

Extend `calculate_routes` with
`geometry_cache: dict | None = None`. Save each selected A/B geometry and
error under the POI `route_id`. Search and recalc each create a fresh
`route_geometries` dictionary, pass it into `calculate_routes`, and save it
to Session only after route calculation succeeds.

Add the async read-only endpoint:

```python
@app.get("/api/v2/session/{session_id}/routes/{route_id}")
async def api_v2_cached_routes(session_id: str, route_id: str):
    session = session_get(session_id)
    if not session:
        return JSONResponse(
            {"success": False, "error": "会话已过期，请重新搜索"},
            status_code=404,
        )
    cached = session.get("route_geometries", {}).get(route_id)
    if cached is None:
        return JSONResponse(
            {"success": False, "error": "未找到该地点的路线缓存"},
            status_code=404,
        )
    return {"success": True, "route_id": route_id, **cached}
```

Keep blocking AMap/route work inside the existing `asyncio.to_thread` calls.
The cached GET endpoint is memory-only and must not call `to_thread`.

- [ ] **Step 4: Run all credential-free deployment checks**

Run:

```bash
venv/bin/python -m unittest discover -s tests -v
venv/bin/python -m py_compile app/main.py app/amap_client.py \
  app/ai_client.py app/config.py
node --test tests/frontend_midpoint_state.test.js \
  tests/frontend_map_routes.test.js
```

Expected: all pure tests and compilation pass. Do not claim a successful
FastAPI AI search because local Runway credentials and FastAPI/Uvicorn
dependencies are absent.

- [ ] **Step 5: Verify deployment configuration was not changed**

Run:

```bash
git diff -- app/config.py app/ai_client.py .env.example
git diff -- app/main.py app/amap_client.py
```

Expected: no diff in `app/config.py` or `app/ai_client.py`, and no new
`DEEPSEEK_API_KEY` reference under `app/`.

- [ ] **Step 6: Final full verification**

Invoke `superpowers:verification-before-completion`, then run fresh:

```bash
venv/bin/python -m unittest discover -s tests -v
node --test tests/frontend_midpoint_state.test.js \
  tests/frontend_map_routes.test.js
venv/bin/python -m py_compile app_v2.py amap_client.py \
  app/main.py app/amap_client.py
git diff --check
git status --short
```

Report:

- local Flask automated and browser results;
- deployment adapter’s credential-free results;
- the explicit limitation that Runway-backed AI search still requires
  the deployment environment;
- every modified/new file, separated from pre-existing workspace changes.
