import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { fixture, gate, write } from './fixtures.mjs';

const rules = ['complexity', 'max-depth', 'max-params', 'no-nested-ternary', 'max-lines', 'max-lines-per-function'];
const mutation = [
  `export function complex(n: number) { ${'if (n) n++; '.repeat(21)} return n; }`,
  'export function deep(n: number) { if(n) { if(n) { if(n) { if(n) { if(n) { n++; } } } } } }',
  'export function parameters(a, b, c, d, e, f) { return [a, b, c, d, e, f]; }',
  'export const ternary = true ? (false ? 1 : 2) : 3;',
  `export function lengthy() {\nlet n = 0;\n${'n++;\n'.repeat(301)}return n;\n}`,
  ...Array.from({ length: 805 }, (_, index) => `export const value${index} = ${index};`),
].join('\n');

for (const source of ['frontend/lib/mutation.ts', 'packages/example/src/mutation.ts']) {
  test(`all six rules reject new violations in ${source}`, t => {
    const path = fixture(t);
    write(path, source, mutation);
    const result = gate(path, 'run-eslint', '--format', 'json');
    assert.equal(result.status, 1, result.output);
    const messages = JSON.parse(result.stdout).flatMap(file => file.messages);
    for (const rule of rules) assert(messages.some(message => message.ruleId === rule), rule);
  });
}

test('native suppression allows the recorded count, rejects growth and requires pruning', t => {
  const path = fixture(t);
  const source = 'frontend/lib/existing.ts';
  const violation = 'export const first = true ? (false ? 1 : 2) : 3;';
  write(path, source, violation);
  assert.equal(gate(path, 'run-eslint', '--suppress-all').status, 0);
  assert.equal(gate(path, 'run-eslint').status, 0);
  write(path, source, `${violation}\nexport const second = true ? (false ? 1 : 2) : 3;`);
  const added = gate(path, 'run-eslint');
  assert.equal(added.status, 1, added.output);
  assert.match(added.output, /no-nested-ternary/);
  write(path, source, 'export const first = 1;');
  const stale = gate(path, 'run-eslint');
  assert.notEqual(stale.status, 0, stale.output);
  assert.match(stale.output, /prune-suppressions/);
  assert.equal(gate(path, 'run-eslint', '--prune-suppressions').status, 0);
  assert.deepEqual(JSON.parse(readFileSync(`${path}/frontend/eslint-suppressions.json`, 'utf8')), {});
});

test('existing React hook and Next rules remain enabled', t => {
  const path = fixture(t);
  write(path, 'frontend/app/page.tsx', `
    import { useState } from 'react';
    export default function Page({ active }) {
      if (active) useState(0);
      return <img src="/image.png" alt="Image" />;
    }
  `);
  const result = gate(path, 'run-eslint');
  assert.equal(result.status, 1, result.output);
  assert.match(result.output, /react-hooks\/rules-of-hooks/);
  assert.match(result.output, /@next\/next\/no-img-element/);
});
