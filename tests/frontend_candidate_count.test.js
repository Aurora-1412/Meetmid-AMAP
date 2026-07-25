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
