import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import ts from '../../../frontend/node_modules/typescript/lib/typescript.js';

const path = 'frontend/features/workspace/paneLayout.ts';
const baselineCommit = '622e485d'; // Characterization commit, before either refactor.
const root = new URL('../../../', import.meta.url);
function load(source) {
  const exports = {};
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } });
  runInNewContext(compiled.outputText, { exports });
  return exports;
}
const before = load(execFileSync('git', ['show', `${baselineCommit}:${path}`], { cwd: root, encoding: 'utf8' }));
const after = load(readFileSync(new URL(path, root), 'utf8'));
let seed = 101;
function random() {
  seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
  return seed / 2 ** 32;
}
const dimensions = [-Infinity, -20, 0, 32, 56, 319.5, 320, 620, 1060, 1280, 1800, Infinity, NaN];
const pickDimension = () => dimensions[Math.floor(random() * dimensions.length)];
let compared = 0;
for (let index = 0; index < 20000; index += 1) {
  const input = structuredClone(before.DEFAULT_WORKSPACE_INPUT);
  input.availableWidth = pickDimension();
  input.mainMinWidth = pickDimension();
  for (const pane of [input.projects, input.files, input.auxiliary, input.inspector]) {
    pane.present = random() > 0.5;
    pane.preferredWidth = pickDimension();
    pane.minWidth = pickDimension();
    pane.maxWidth = pickDimension();
  }
  input.projects.collapsed = random() > 0.5;
  input.projects.collapsedWidth = pickDimension();
  input.auxiliary.open = random() > 0.5;
  input.inspector.open = random() > 0.5;
  if (random() > 0.3) {
    input.segments = {
      orientation: random() > 0.5 ? 'horizontal' : 'vertical',
      first: pickDimension(), second: pickDimension(), gap: pickDimension(),
    };
  }
  assert.deepStrictEqual(
    structuredClone(after.resolveWorkspaceLayout(input)),
    structuredClone(before.resolveWorkspaceLayout(input)),
    `Layout differs from ${baselineCommit} at seeded case ${index}`,
  );
  compared += 1;
}
console.log(`PASS: ${compared} seeded edge cases match ${baselineCommit}, including non-finite dimensions and segments.`);
