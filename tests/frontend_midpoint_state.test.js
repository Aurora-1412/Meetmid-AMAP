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
