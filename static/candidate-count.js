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
