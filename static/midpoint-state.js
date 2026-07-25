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
