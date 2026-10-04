import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { command, fixture, gate, write } from './fixtures.mjs';

const forbidden = [
  ['frontend/lib/a.ts', 'components/b', 'lib-no-application'],
  ['frontend/lib/a.ts', 'contexts/b', 'lib-no-application'],
  ['frontend/lib/a.ts', 'features/b', 'lib-no-application'],
  ['frontend/lib/a.ts', 'app/b', 'lib-no-application'],
  ['frontend/components/a.ts', 'features/b', 'components-no-features'],
  ['frontend/features/a.ts', 'app/b', 'features-no-routes'],
  ['frontend/shared-ui/src/a.ts', 'app/b', 'packages-no-application'],
  ['frontend/packages/example/a.ts', 'lib/b', 'packages-no-application'],
  ['packages/example/src/a.ts', 'features/b', 'packages-no-application'],
  ['packages/example/src/a.ts', 'middleware', 'packages-no-application'],
];

for (const [source, target, rule] of forbidden) {
  test(`${source} cannot import @/${target} (including type-only imports)`, t => {
    const path = fixture(t);
    write(path, source, `import type { Value } from '@/${target}'; export type Copy = Value;`);
    write(path, `frontend/${target}.ts`, 'export type Value = string;');
    const result = gate(path, 'run-dependencies');
    assert.equal(result.status, 1, result.output);
    assert.match(result.output, new RegExp(rule));
  });
}

test('relative dynamic imports cannot bypass the package boundary', t => {
  const path = fixture(t);
  write(path, 'packages/example/a.ts', "export const load = () => import('../../frontend/lib/b');");
  write(path, 'frontend/lib/b.ts', 'export const value = 1;');
  const result = gate(path, 'run-dependencies');
  assert.equal(result.status, 1, result.output);
  assert.match(result.output, /packages-no-application/);
});

test('allowed directions resolve app aliases and root package aliases', t => {
  const path = fixture(t);
  write(path, 'frontend/app/page.ts', "export { value } from '@/features/a';");
  write(path, 'frontend/features/a.ts', "export { value } from '@puppyone/data-core';");
  write(path, 'packages/data-core/src/index.ts', 'export const value = 1;');
  write(path, 'packages/example/src/index.ts', "export { value } from '@puppyone/data-core';");
  const result = gate(path, 'run-dependencies');
  assert.equal(result.status, 0, result.output);
  assert.match(result.output, /0 dependency violations/);
});

test('unresolved local aliases fail instead of hiding dependency edges', t => {
  const path = fixture(t);
  write(path, 'frontend/lib/a.ts', "export { value } from '@/features/missing';");
  const result = gate(path, 'run-dependencies');
  assert.equal(result.status, 1, result.output);
  assert.match(result.output, /no-unresolved-local/);
});

function recordExisting(path) {
  const result = command(path, process.execPath, [
    'frontend/node_modules/dependency-cruiser/bin/dependency-cruise.mjs',
    '--config', 'frontend/.dependency-cruiser.cjs', '--output-type', 'baseline', 'frontend',
  ]);
  assert.equal(result.status, 0, result.output);
  write(path, 'frontend/.dependency-cruiser-known-violations.json', JSON.parse(result.stdout));
}

test('known cycle passes; a new cycle fails; removed cycles require pruning', t => {
  const path = fixture(t);
  write(path, 'frontend/features/a.ts', "export { b } from './b'; export const a = 1;");
  write(path, 'frontend/features/b.ts', "export { a } from './a'; export const b = 2;");
  const initial = gate(path, 'run-dependencies');
  assert.equal(initial.status, 1, initial.output);
  assert.match(initial.output, /no-circular/);
  recordExisting(path);
  assert.equal(gate(path, 'run-dependencies').status, 0);
  write(path, 'frontend/features/c.ts', "export { d } from './d'; export const c = 1;");
  write(path, 'frontend/features/d.ts', "export { c } from './c'; export const d = 2;");
  assert.equal(gate(path, 'run-dependencies').status, 1);
  write(path, 'frontend/features/a.ts', 'export const a = 1;');
  write(path, 'frontend/features/c.ts', 'export const c = 1;');
  const stale = gate(path, 'run-dependencies');
  assert.equal(stale.status, 1, stale.output);
  assert.match(stale.output, /stale dependency entries/);
  assert.equal(gate(path, 'run-dependencies', '--prune').status, 0);
  assert.deepEqual(JSON.parse(readFileSync(`${path}/frontend/.dependency-cruiser-known-violations.json`, 'utf8')), []);
});
