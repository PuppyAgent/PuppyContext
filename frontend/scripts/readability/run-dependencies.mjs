import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { violationKey } from './baselines.mjs';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const baselinePath = 'frontend/.dependency-cruiser-known-violations.json';
const baseline = JSON.parse(readFileSync(`${root}/${baselinePath}`, 'utf8'));
const inputs = ['frontend', 'packages'].filter(path => existsSync(`${root}/${path}`));
const result = spawnSync(process.execPath, [
  'frontend/node_modules/dependency-cruiser/bin/dependency-cruise.mjs',
  '--config', 'frontend/.dependency-cruiser.cjs', '--ignore-known', baselinePath,
  '--output-type', 'json', ...inputs,
], { cwd: root, encoding: 'utf8', maxBuffer: 20 * 1024 * 1024 });
if (result.error) throw result.error;
if (result.status !== 0) throw new Error(result.stderr || result.stdout);
const report = JSON.parse(result.stdout);
const current = report.summary.violations;
const knownKeys = new Set(baseline.map(violationKey));
const currentKeys = new Set(current.map(violationKey));
const added = current.filter(item => !knownKeys.has(violationKey(item)));
const stale = baseline.filter(item => !currentKeys.has(violationKey(item)));

if (process.argv.includes('--prune')) {
  const retained = baseline.filter(item => currentKeys.has(violationKey(item)));
  writeFileSync(`${root}/${baselinePath}`, `${JSON.stringify(retained, null, 2)}\n`);
} else if (stale.length) {
  console.error(`${stale.length} stale dependency entries. Run npm run dependencies:prune.`);
  process.exitCode = 1;
}
for (const item of added) console.error(`${item.rule.name}: ${item.from} -> ${item.to}`);
if (added.length || current.some(item => item.rule.severity === 'error')) process.exitCode = 1;
console.log(`${report.modules.length} modules; ${current.length} dependency violations; ${added.length} new.`);
