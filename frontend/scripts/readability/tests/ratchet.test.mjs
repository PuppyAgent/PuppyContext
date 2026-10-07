import assert from 'node:assert/strict';
import test from 'node:test';
import { assertShrinks, dependencyCounts, suppressionCounts } from '../baselines.mjs';
import { command, fixture, gate, write } from './fixtures.mjs';

const lint = { 'frontend/lib/a.ts': { complexity: { count: 2 } } };
const edge = { type: 'dependency', from: 'frontend/lib/a.ts', to: 'frontend/app/b.ts', rule: { name: 'lib-no-application' } };

test('suppression ratchet rejects count increases and equal-total entry swaps', () => {
  const previous = suppressionCounts(lint);
  assertShrinks(previous, suppressionCounts({ 'frontend/lib/a.ts': { complexity: { count: 1 } } }), 'ESLint');
  assert.throws(() => assertShrinks(previous, suppressionCounts({
    'frontend/lib/a.ts': { complexity: { count: 3 } },
  }), 'ESLint'), /baseline enlarged/);
  assert.throws(() => assertShrinks(previous, suppressionCounts({
    'frontend/lib/new.ts': { complexity: { count: 2 } },
  }), 'ESLint'), /baseline enlarged/);
  assert.throws(() => suppressionCounts({ a: { complexity: { count: -1 } } }), /Invalid/);
});

test('dependency ratchet rejects new edges, duplicate entries and changed cycles', () => {
  const previous = dependencyCounts([edge]);
  assertShrinks(previous, dependencyCounts([]), 'Dependencies');
  assert.throws(() => assertShrinks(previous, dependencyCounts([edge, edge]), 'Dependencies'), /enlarged/);
  assert.throws(() => assertShrinks(previous, dependencyCounts([{ ...edge, to: 'frontend/app/new.ts' }]), 'Dependencies'), /enlarged/);
  const cycle = { ...edge, type: 'cycle', cycle: [{ name: 'a' }, { name: 'b' }, { name: 'c' }] };
  assert.throws(() => assertShrinks(dependencyCounts([cycle]), dependencyCounts([
    { ...cycle, cycle: [{ name: 'a' }, { name: 'c' }, { name: 'b' }] },
  ]), 'Dependencies'), /enlarged/);
});

test('CI command compares against Git base, including decreased counts', t => {
  const path = fixture(t);
  const lintPath = 'frontend/eslint-suppressions.json';
  const dependencyPath = 'frontend/.dependency-cruiser-known-violations.json';
  write(path, lintPath, lint);
  write(path, dependencyPath, [edge]);
  for (const args of [
    ['init', '--quiet'], ['add', lintPath, dependencyPath],
    ['-c', 'user.name=Readability Test', '-c', 'user.email=test@example.invalid', 'commit', '--quiet', '-m', 'test: fixture baseline'],
  ]) assert.equal(command(path, 'git', args).status, 0);
  assert.equal(gate(path, 'check-baselines', 'HEAD').status, 0);
  write(path, lintPath, { 'frontend/lib/a.ts': { complexity: { count: 1 } } });
  assert.equal(gate(path, 'check-baselines', 'HEAD').status, 0);
  assert.equal(command(path, 'git', ['add', lintPath]).status, 0);
  assert.equal(command(path, 'git', [
    '-c', 'user.name=Readability Test', '-c', 'user.email=test@example.invalid',
    'commit', '--quiet', '-m', 'test: ratchet reduction',
  ]).status, 0);
  write(path, lintPath, lint);
  assert.equal(gate(path, 'check-baselines', 'HEAD').status, 1);
  write(path, lintPath, {});
  write(path, dependencyPath, [edge, { ...edge, to: 'frontend/app/new.ts' }]);
  const increased = gate(path, 'check-baselines', 'HEAD');
  assert.equal(increased.status, 1, increased.output);
  assert.match(increased.output, /baseline enlarged/);
  assert.equal(gate(path, 'check-baselines', 'missing-ref').status, 1);
});
