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
