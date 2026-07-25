# Candidate Count Control Styling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the candidate-count number input visually match the existing dark form while reducing its label and hint typography.

**Architecture:** Keep the adjustment local to `static/index.html` by adding four candidate-count-specific CSS classes and applying them to the existing markup. Add a small static contract test that locks the approved color tokens, typography, spacing, and unchanged numeric attributes.

**Tech Stack:** HTML, CSS, Node.js `node:test`, and the existing local Flask/browser verification flow.

## Global Constraints

- Change styling only; do not change candidate-count validation, default, range, request payload, or backend behavior.
- The group top margin is 12px.
- The label is 12px, weight 600, `var(--text2)`, with 6px below it.
- The input is full width with border-box sizing, `7px 10px` padding, 13px text, `var(--surface)` background, `var(--text)` text, `1px solid var(--border)`, and 8px radius.
- Input focus uses `var(--primary)` for the border.
- The hint is 11px, `var(--text2)`, with 5px above it.
- The input remains default 10 with integer range 1–20.
- Preserve unrelated dirty files and commit only files belonging to this styling task.

---

### Task 1: Add the compact themed candidate-count styling

**Files:**
- Create: `tests/frontend_candidate_count_style.test.js`
- Modify: `static/index.html:395-460`
- Modify: `static/index.html:922-937`

**Interfaces:**
- Produces CSS classes: `.candidate-count-group`, `.candidate-count-label`, `.candidate-count-input`, `.candidate-count-hint`
- Preserves DOM ID: `candidateCountInput`

- [ ] **Step 1: Write the failing static style contract**

Create `tests/frontend_candidate_count_style.test.js`:

```javascript
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'index.html'),
  'utf8',
);

test('candidate count control uses compact themed styles', () => {
  assert.match(html, /\.candidate-count-group\s*\{[^}]*margin-top:\s*12px/s);
  const label = html.match(/\.candidate-count-label\s*\{[^}]*\}/s)?.[0];
  assert.match(label, /font-size:\s*12px/);
  assert.match(label, /font-weight:\s*600/);
  assert.match(label, /color:\s*var\(--text2\)/);
  assert.match(label, /margin-bottom:\s*6px/);
  assert.match(
    html,
    /\.candidate-count-input\s*\{[^}]*width:\s*100%[^}]*box-sizing:\s*border-box[^}]*padding:\s*7px 10px[^}]*background:\s*var\(--surface\)[^}]*border:\s*1px solid var\(--border\)[^}]*border-radius:\s*8px[^}]*color:\s*var\(--text\)[^}]*font-size:\s*13px/s,
  );
  assert.match(
    html,
    /\.candidate-count-input:focus\s*\{[^}]*border-color:\s*var\(--primary\)/s,
  );
  const hint = html.match(/\.candidate-count-hint\s*\{[^}]*\}/s)?.[0];
  assert.match(hint, /font-size:\s*11px/);
  assert.match(hint, /color:\s*var\(--text2\)/);
  assert.match(hint, /margin-top:\s*5px/);
});

test('candidate count numeric contract remains unchanged', () => {
  const input = html.match(
    /<input[\s\S]*?id="candidateCountInput"[\s\S]*?>/,
  )?.[0];
  assert.ok(input);
  assert.match(input, /type="number"/);
  assert.match(input, /min="1"/);
  assert.match(input, /max="20"/);
  assert.match(input, /step="1"/);
  assert.match(input, /value="10"/);
});
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
node --test tests/frontend_candidate_count_style.test.js
```

Expected: the style test FAILS because the dedicated CSS classes do not exist.

- [ ] **Step 3: Add the candidate-count-specific CSS**

Add near the existing search/input styles in `static/index.html`:

```css
.candidate-count-group {
  margin-top: 12px;
}
.candidate-count-label {
  display: block;
  margin-bottom: 6px;
  color: var(--text2);
  font-size: 12px;
  font-weight: 600;
}
.candidate-count-input {
  width: 100%;
  box-sizing: border-box;
  padding: 7px 10px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text);
  font-size: 13px;
  outline: none;
  transition: border-color 0.2s;
}
.candidate-count-input:focus {
  border-color: var(--primary);
}
.candidate-count-hint {
  margin-top: 5px;
  color: var(--text2);
  font-size: 11px;
}
```

- [ ] **Step 4: Apply the dedicated classes to the existing markup**

Replace only the current undefined class names:

```html
<div class="candidate-count-group">
  <label class="candidate-count-label" for="candidateCountInput">
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
    class="candidate-count-input"
  >
  <div class="candidate-count-hint">可输入 1–20，数量越少计算越快</div>
</div>
```

- [ ] **Step 5: Run focused and full frontend tests**

Run:

```bash
node --test \
  tests/frontend_candidate_count_style.test.js \
  tests/frontend_candidate_count.test.js \
  tests/frontend_midpoint_state.test.js \
  tests/frontend_map_routes.test.js
git diff --check -- static/index.html \
  tests/frontend_candidate_count_style.test.js
```

Expected: 10 tests PASS and the diff check exits 0.

- [ ] **Step 6: Commit only the style implementation and test**

```bash
git add static/index.html tests/frontend_candidate_count_style.test.js
git commit -m "style: align candidate count input"
```

---

### Task 2: Browser visual verification

**Files:**
- Verify: `static/index.html`
- Verify: `tests/frontend_candidate_count_style.test.js`

**Interfaces:**
- Consumes: CSS classes produced by Task 1
- Produces: computed-style and regression evidence

- [ ] **Step 1: Start the local Flask entrypoint**

Run:

```bash
venv/bin/python app_v2.py
```

Expected: local page is available at `http://127.0.0.1:5000`.

- [ ] **Step 2: Inspect computed styles in the browser**

Open the local page and inspect the live elements. Confirm:

```text
#candidateCountInput value                 = "10"
#candidateCountInput background-color      = rgb(26, 29, 38)
#candidateCountInput color                 = rgb(232, 234, 240)
#candidateCountInput font-size             = 13px
#candidateCountInput border-radius         = 8px
.candidate-count-label font-size           = 12px
.candidate-count-label font-weight         = 600
.candidate-count-hint font-size            = 11px
```

Confirm the input is visually aligned with the departure-time field and no
browser errors are logged.

- [ ] **Step 3: Confirm functionality is unchanged**

Change the value to 3 and then back to 10. Confirm the control accepts the
same 1–20 integer range and no JavaScript error occurs. A new real AI search
is not required because this task does not change request logic.

- [ ] **Step 4: Stop the server and run final checks**

Stop Flask with `Ctrl-C`, then run:

```bash
node --test \
  tests/frontend_candidate_count_style.test.js \
  tests/frontend_candidate_count.test.js \
  tests/frontend_midpoint_state.test.js \
  tests/frontend_map_routes.test.js
git diff --check
git status --short
```

Expected: 10 tests PASS; only the already-known unrelated dirty files remain.
