// @vitest-environment node
import { expect, it } from 'vitest';
import { analyzeHeap } from '../../scripts/server-memory-analysis.mjs';
const MB = 2 ** 20;
const samples = (heaps: number[]) => heaps.map((heap, i) => ({ heapUsed: heap * MB, timestamp: i * 1000 }));
it('accepts a stable post-GC baseline with bounded noise', () => {
  expect(analyzeHeap(samples([100, 101, 100, 99, 102, 101, 100, 101, 100]))).toMatchObject({ passed: true, growthBytes: 0 });
});
it('detects retained growth rather than only enforcing an absolute memory ceiling', () => {
  expect(analyzeHeap(samples([100, 110, 120, 130, 140, 150, 160, 170, 180]))).toMatchObject({ passed: false, growthBytes: 60 * MB, bytesPerSecond: 10 * MB });
});
it('is robust to an isolated transient spike', () => {
  expect(analyzeHeap(samples([100, 100, 100, 400, 100, 100, 100, 400, 100]))).toMatchObject({ passed: true, growthBytes: 0 });
});
it('rejects undersampled or malformed receipts', () => {
  expect(() => analyzeHeap(samples([100, 101]))).toThrow('six');
  expect(() => analyzeHeap(samples([100, 100, 100, NaN, 100, 100]))).toThrow('Invalid');
});
