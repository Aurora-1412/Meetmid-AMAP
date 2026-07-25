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
      next() {
        current += 1;
        return current;
      },
      invalidate() {
        current += 1;
      },
      isCurrent(token) {
        return token === current;
      },
    };
  }

  return { buildDrawableRoutes, createRequestGate };
});
