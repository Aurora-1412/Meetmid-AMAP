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
