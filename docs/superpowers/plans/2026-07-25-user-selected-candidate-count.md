# User-Selected Candidate Count Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users enter a candidate count from 1 through 20 before searching, default it to 10, and make both backends calculate only that many candidates.

**Architecture:** Add one framework-independent Python validator shared by the Flask and FastAPI entrypoints, then pass the validated value through each search pipeline so it overrides the LLM-proposed `top_n`. Add one small browser/CommonJS module for front-end validation, replace the result-only display selector with the search-time number input, and retain the existing Session and route-cache behavior for only the selected candidates.

**Tech Stack:** Python 3.10, Flask, FastAPI, unittest, vanilla JavaScript, Node.js `node:test`, and the existing AMap browser integration.

## Global Constraints

- The accepted range is exactly 1–20 inclusive.
- The default is exactly 10 in the browser, Flask API, FastAPI API, and direct pipeline calls.
- Booleans, strings, floats, empty input, zero, negative numbers, and values over 20 are invalid.
- Invalid API input returns HTTP 400 with `候选点数量必须是 1 到 20 之间的整数` before any LLM or AMap call.
- `candidate_count` overrides the LLM plan’s `top_n`.
- Search responses and Sessions retain `candidate_count`; route recalculation does not change it.
- Remove the existing result-page “显示 5/10/15/20 个” selector.
- Preserve all pre-existing uncommitted changes. Do not stage or commit implementation files without separate user authorization.
- Implement and verify the local `app_v2.py` path first, then sync to `app/main.py`; the FastAPI/Runway entrypoint receives offline compile and contract verification only.

---

### Task 1: Shared candidate-count contract and local Flask pipeline

**Files:**
- Create: `candidate_count.py`
- Create: `tests/test_candidate_count.py`
- Modify: `app_v2.py:120-175`
- Modify: `app_v2.py:632-728`
- Modify: `app_v2.py:748-790`
- Modify: `app_v2.py:900-925`

**Interfaces:**
- Produces: `parse_candidate_count(value: object = None) -> int`
- Extends: `run_pipeline(..., midpoint_mode: str = "geo", candidate_count: int = 10) -> dict`
- Extends: `POST /api/v2/search` with optional JSON field `candidate_count`

- [ ] **Step 1: Write failing validator tests**

Create `tests/test_candidate_count.py`:

```python
import unittest
from unittest.mock import patch

import app_v2
from candidate_count import parse_candidate_count


class CandidateCountTest(unittest.TestCase):
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
```

- [ ] **Step 2: Run the validator tests and verify RED**

Run:

```bash
venv/bin/python -m unittest tests.test_candidate_count -v
```

Expected: FAIL because `candidate_count.py` does not exist.

- [ ] **Step 3: Implement the shared validator**

Create `candidate_count.py`:

```python
CANDIDATE_COUNT_DEFAULT = 10
CANDIDATE_COUNT_MIN = 1
CANDIDATE_COUNT_MAX = 20
CANDIDATE_COUNT_ERROR = "候选点数量必须是 1 到 20 之间的整数"


def parse_candidate_count(value: object = None) -> int:
    if value is None:
        return CANDIDATE_COUNT_DEFAULT
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not CANDIDATE_COUNT_MIN <= value <= CANDIDATE_COUNT_MAX
    ):
        raise ValueError(CANDIDATE_COUNT_ERROR)
    return value
```

- [ ] **Step 4: Validate Flask requests before service calls**

Import the helper in `app_v2.py`:

```python
from candidate_count import parse_candidate_count
```

In `api_v2_search`, immediately after reading the request fields:

```python
try:
    candidate_count = parse_candidate_count(data.get("candidate_count"))
except ValueError as error:
    return jsonify({"success": False, "error": str(error)}), 400
```

Pass it to the pipeline as the final positional argument:

```python
result = run_pipeline(
    user_query, location_a, location_b,
    city, prefer_a, prefer_b, departure_time, midpoint_mode,
    candidate_count,
)
```

- [ ] **Step 5: Write a failing pipeline-override test**

Append to `CandidateCountTest`:

```python
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
```

- [ ] **Step 6: Run the override test and verify RED**

Run:

```bash
venv/bin/python -m unittest \
  tests.test_candidate_count.CandidateCountTest.test_user_count_overrides_llm_top_n \
  -v
```

Expected: FAIL because `run_pipeline` does not accept `candidate_count`.

- [ ] **Step 7: Pass the value through the Flask pipeline**

Extend the signature:

```python
def run_pipeline(
    user_query: str,
    location_a: dict, location_b: dict,
    city: str = "北京",
    prefer_a: str = "auto", prefer_b: str = "auto",
    departure_time: str | None = None,
    midpoint_mode: str = "geo",
    candidate_count: int = 10,
) -> dict:
```

At the top of the function, normalize direct calls and override the plan:

```python
candidate_count = parse_candidate_count(candidate_count)
plan = agent_plan(user_query)
plan["top_n"] = candidate_count
```

Use the count in filtering:

```python
top_pois = filter_and_rank_pois(
    search_ctx["pois"],
    min_rating=plan.get("min_rating", 4.0),
    top_n=candidate_count,
)
```

Add `"candidate_count": candidate_count` to both the Session payload and
the search response. Add it to `api_v2_session_info`:

```python
"candidate_count": session.get("candidate_count", 10),
```

- [ ] **Step 8: Run the local candidate-count tests and verify GREEN**

Run:

```bash
venv/bin/python -m unittest tests.test_candidate_count -v
```

Expected: all candidate-count tests PASS.

- [ ] **Step 9: Review the local backend checkpoint**

Run:

```bash
git diff --check -- candidate_count.py app_v2.py tests/test_candidate_count.py
git diff -- candidate_count.py app_v2.py tests/test_candidate_count.py
```

Confirm that search defaults to 10, invalid counts fail before `run_pipeline`,
and neither midpoint nor route-cache behavior changed.

---

### Task 2: Search-time numeric input in the shared frontend

**Files:**
- Create: `static/candidate-count.js`
- Create: `tests/frontend_candidate_count.test.js`
- Modify: `static/index.html:820-1010`
- Modify: `static/index.html:1110-1160`
- Modify: `static/index.html:1730-2020`

**Interfaces:**
- Consumes: API field `candidate_count: int`
- Produces: `CandidateCount.parse(raw: unknown) -> {valid: boolean, value?: number, error?: string}`
- Removes: `displayCountSelect`, `onDisplayCountChange()`

- [ ] **Step 1: Write failing browser-module tests**

Create `tests/frontend_candidate_count.test.js`:

```javascript
const test = require('node:test');
const assert = require('node:assert/strict');
const CandidateCount = require('../static/candidate-count.js');

test('defaults to ten only when no value is supplied programmatically', () => {
  assert.deepEqual(CandidateCount.parse(undefined), {
    valid: true,
    value: 10,
  });
});

test('accepts integer strings from one through twenty', () => {
  assert.equal(CandidateCount.parse('1').value, 1);
  assert.equal(CandidateCount.parse('10').value, 10);
  assert.equal(CandidateCount.parse('20').value, 20);
});

test('rejects empty, fractional, and out-of-range input', () => {
  ['', '0', '21', '3.5', 'abc'].forEach((raw) => {
    const result = CandidateCount.parse(raw);
    assert.equal(result.valid, false, raw);
    assert.equal(
      result.error,
      '候选点数量必须是 1 到 20 之间的整数',
    );
  });
});
```

- [ ] **Step 2: Run the browser-module test and verify RED**

Run:

```bash
node --test tests/frontend_candidate_count.test.js
```

Expected: FAIL because `static/candidate-count.js` does not exist.

- [ ] **Step 3: Implement the pure front-end validator**

Create `static/candidate-count.js`:

```javascript
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.CandidateCount = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  const ERROR = '候选点数量必须是 1 到 20 之间的整数';

  function parse(raw) {
    if (raw === undefined) return { valid: true, value: 10 };
    if (typeof raw !== 'string' && typeof raw !== 'number') {
      return { valid: false, error: ERROR };
    }
    const text = String(raw).trim();
    const value = Number(text);
    if (
      text === ''
      || !Number.isInteger(value)
      || value < 1
      || value > 20
    ) {
      return { valid: false, error: ERROR };
    }
    return { valid: true, value };
  }

  return { parse };
});
```

- [ ] **Step 4: Run the browser-module test and verify GREEN**

Run:

```bash
node --test tests/frontend_candidate_count.test.js
```

Expected: 3 tests PASS.

- [ ] **Step 5: Add the search-time number input**

In `static/index.html`, add a form group immediately before the departure-time
group:

```html
<div class="form-group">
  <label class="form-label" for="candidateCountInput">
    📌 候选点数量
  </label>
  <input
    id="candidateCountInput"
    type="number"
    min="1"
    max="20"
    step="1"
    value="10"
    inputmode="numeric"
    class="form-input"
  >
  <div class="form-hint">可输入 1–20，数量越少计算越快</div>
</div>
```

Load the pure module before the page’s inline script:

```html
<script src="static/candidate-count.js"></script>
<script src="static/midpoint-state.js"></script>
<script src="static/map-routes.js"></script>
```

- [ ] **Step 6: Validate and send the count from `doSearch`**

After validating the query:

```javascript
const countResult = CandidateCount.parse(
  document.getElementById('candidateCountInput').value
);
if (!countResult.valid) {
  showToast(countResult.error, 'error');
  return;
}
```

Add to the JSON body:

```javascript
candidate_count: countResult.value,
```

- [ ] **Step 7: Remove the result-only quantity selector**

Delete the `displayCountSelect` markup. In `renderResults`, delete the selector
reset and replace:

```javascript
renderPoisByCount(
  parseInt(document.getElementById('displayCountSelect')?.value || '20')
);
```

with:

```javascript
renderPoisByCount(allPoisData.length);
```

Delete `onDisplayCountChange()`. Keep `renderPoisByCount` because it remains
the single renderer for cards and markers.

- [ ] **Step 8: Run front-end tests and syntax checks**

Run:

```bash
node --test \
  tests/frontend_candidate_count.test.js \
  tests/frontend_midpoint_state.test.js \
  tests/frontend_map_routes.test.js
node --check static/candidate-count.js
node --check static/midpoint-state.js
node --check static/map-routes.js
```

Expected: all tests PASS and all syntax checks exit 0.

- [ ] **Step 9: Review the frontend checkpoint**

Run:

```bash
rg -n "candidateCountInput|candidate_count|displayCountSelect|onDisplayCountChange" \
  static/index.html static/candidate-count.js
git diff --check -- static/index.html static/candidate-count.js \
  tests/frontend_candidate_count.test.js
```

Confirm the new input defaults to 10 and the old result selector no longer
exists.

---

### Task 3: FastAPI/Runway deployment parity

**Files:**
- Modify: `app/main.py:610-715`
- Modify: `app/main.py:745-920`
- Test: `tests/test_candidate_count.py`

**Interfaces:**
- Consumes: `parse_candidate_count(value: object = None) -> int`
- Extends: deployment `run_pipeline(..., candidate_count: int = 10) -> dict`
- Extends: deployment `POST /api/v2/search` with `candidate_count`

- [ ] **Step 1: Add an offline deployment-source contract test**

Append to `CandidateCountTest`:

```python
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
```

- [ ] **Step 2: Run the deployment contract test and verify RED**

Run:

```bash
venv/bin/python -m unittest \
  tests.test_candidate_count.CandidateCountTest.test_deployment_entrypoint_uses_shared_candidate_contract \
  -v
```

Expected: FAIL because `app/main.py` does not import or use the shared
candidate-count contract.

- [ ] **Step 3: Apply the shared contract to FastAPI**

Import:

```python
from candidate_count import parse_candidate_count
```

Extend deployment `run_pipeline` with `candidate_count: int = 10`, normalize
it, set `plan["top_n"] = candidate_count`, use it in
`filter_and_rank_pois`, and store/return it exactly as in the Flask pipeline.

In `api_v2_search`, validate before the AMAP and AI configuration checks:

```python
try:
    candidate_count = parse_candidate_count(data.get("candidate_count"))
except ValueError as error:
    return JSONResponse(
        {"success": False, "error": str(error)},
        status_code=400,
    )
```

Pass `candidate_count` as the final argument to `run_pipeline`. Include it
in `api_v2_session_info`:

```python
"candidate_count": s.get("candidate_count", 10),
```

- [ ] **Step 4: Run offline deployment verification**

Run:

```bash
venv/bin/python -m unittest tests.test_candidate_count -v
venv/bin/python -m py_compile app/main.py candidate_count.py
```

Expected: all tests PASS and compilation exits 0. Do not claim a FastAPI
end-to-end run because the local environment lacks FastAPI and Runway
credentials.

- [ ] **Step 5: Review local/deployment parity**

Run:

```bash
rg -n "candidate_count|parse_candidate_count" app_v2.py app/main.py
git diff --check -- candidate_count.py app_v2.py app/main.py \
  tests/test_candidate_count.py
```

Confirm both APIs default to 10, reject the same invalid values, and expose
the same response and Session field.

---

### Task 4: Full regression and local browser verification

**Files:**
- Verify: `candidate_count.py`
- Verify: `app_v2.py`
- Verify: `app/main.py`
- Verify: `static/index.html`
- Verify: `static/candidate-count.js`
- Verify: `tests/`

**Interfaces:**
- Consumes: all interfaces from Tasks 1–3
- Produces: verified user flow and final evidence

- [ ] **Step 1: Run the complete automated suite**

Run:

```bash
venv/bin/python -m unittest discover -s tests -v
node --test \
  tests/frontend_candidate_count.test.js \
  tests/frontend_midpoint_state.test.js \
  tests/frontend_map_routes.test.js
venv/bin/python -m py_compile \
  candidate_count.py app_v2.py amap_client.py app/main.py app/amap_client.py
node --check static/candidate-count.js
node --check static/midpoint-state.js
node --check static/map-routes.js
git diff --check
```

Expected: every test passes and every compile, syntax, and diff check exits 0.

- [ ] **Step 2: Start the verified local Flask entrypoint**

Run:

```bash
venv/bin/python app_v2.py
```

Expected: Flask listens at `http://127.0.0.1:5000`.

- [ ] **Step 3: Verify default and invalid-input behavior in the browser**

Open `http://127.0.0.1:5000` and confirm:

- `candidateCountInput` initially contains `10`.
- Changing it to `21` and attempting search shows
  `候选点数量必须是 1 到 20 之间的整数`.
- No `POST /api/v2/search` occurs for the invalid attempt.

- [ ] **Step 4: Verify a reduced real search**

Choose two locations, enter a query, set candidate count to `3`, and search.
Confirm:

- The result header says `共 3 个`.
- Exactly three result cards and three candidate markers render.
- Clicking a card still reads its cached A/B routes and shows the blue/orange
  legend.
- The initial search response does not contain route geometry arrays.
- Browser error logs are empty.

- [ ] **Step 5: Stop the local server and inspect final ownership**

Stop Flask with `Ctrl-C`, then run:

```bash
git status --short
rg -n "candidate_count|candidateCountInput" \
  candidate_count.py app_v2.py app/main.py static/index.html tests
```

Confirm the only new files for this feature are:

- `candidate_count.py`
- `static/candidate-count.js`
- `tests/test_candidate_count.py`
- `tests/frontend_candidate_count.test.js`
- this design and plan document

Do not update or deploy the pre-existing `midpoint-recommender.zip`; it contains
an embedded `.env` and does not reflect the current source tree.
