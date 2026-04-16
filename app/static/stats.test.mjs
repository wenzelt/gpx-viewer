// TDD tests for DE-style number formatting and fullscreen toggle logic
// Run with: node --test app/static/stats.test.js

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';

// --- Module under test (inline copy for Node test runner) ---
// These functions mirror what will live in the HTML <script> block.

function formatDeNumber(value, decimals = 0) {
  return new Intl.NumberFormat('de-DE', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value);
}

function formatDistance(totalDistanceM) {
  if (totalDistanceM == null) return '—';
  const distKm = totalDistanceM / 1000;
  if (distKm >= 1) {
    return formatDeNumber(distKm, 1) + '\u202fkm';
  }
  return formatDeNumber(Math.round(totalDistanceM)) + '\u202fm';
}

function formatElevation(totalElevationGainM) {
  if (totalElevationGainM == null) return '—';
  return formatDeNumber(Math.round(totalElevationGainM)) + '\u202fm';
}

// ─────────────────────────────────────────────────────
// Tests
// ─────────────────────────────────────────────────────

describe('formatDeNumber', () => {
  test('formats thousands with DE period separator', () => {
    assert.equal(formatDeNumber(1234), '1.234');
  });

  test('formats millions with DE period separators', () => {
    assert.equal(formatDeNumber(1234567), '1.234.567');
  });

  test('formats decimals with DE comma separator', () => {
    assert.equal(formatDeNumber(1234.5, 1), '1.234,5');
  });

  test('formats zero correctly', () => {
    assert.equal(formatDeNumber(0), '0');
  });

  test('formats sub-1000 without separator', () => {
    assert.equal(formatDeNumber(999), '999');
  });
});

describe('formatDistance', () => {
  test('returns em-dash for null input', () => {
    assert.equal(formatDistance(null), '—');
  });

  test('formats distances >= 1000 m as km with DE decimal comma', () => {
    // 1500 m → 1,5 km
    assert.equal(formatDistance(1500), '1,5\u202fkm');
  });

  test('formats large distance with thousands separator', () => {
    // 12345678 m → 12.345,7 km
    assert.equal(formatDistance(12345678), '12.345,7\u202fkm');
  });

  test('formats distances < 1000 m as metres with DE thousands separator', () => {
    // 850 m
    assert.equal(formatDistance(850), '850\u202fm');
  });

  test('formats exactly 1000 m as 1,0 km', () => {
    assert.equal(formatDistance(1000), '1,0\u202fkm');
  });
});

describe('formatElevation', () => {
  test('returns em-dash for null input', () => {
    assert.equal(formatElevation(null), '—');
  });

  test('formats elevation with DE thousands separator', () => {
    // 1234 m elevation gain
    assert.equal(formatElevation(1234), '1.234\u202fm');
  });

  test('rounds decimal elevation', () => {
    assert.equal(formatElevation(456.7), '457\u202fm');
  });

  test('formats zero elevation', () => {
    assert.equal(formatElevation(0), '0\u202fm');
  });
});
