import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const read = name => JSON.parse(readFileSync(new URL(`${name}.json`, import.meta.url), 'utf8'));
const input = new Set(Object.keys(read('source-before')));
const defaultDirectories = new Set(['app', 'pages', 'components', 'lib', 'src']);

function diagnostics(rows, defaultScope = false) {
  return rows
    .filter(row => input.has(row.filePath) && /\.(js|jsx|ts|tsx)$/.test(row.filePath))
    .filter(row => !defaultScope || defaultDirectories.has(row.filePath.split('/')[1]))
    .map(row => ({ filePath: row.filePath, messages: row.messages }))
    .sort((left, right) => left.filePath.localeCompare(right.filePath));
}

for (const name of ['legacy-default', 'legacy-full']) {
  const before = diagnostics(read(name));
  const after = diagnostics(read('flat-full'), name === 'legacy-default');
  assert.deepEqual(after, before);
  console.log(`${name}: identical messages, positions, severity, suggestions and fixes on the captured input.`);
}
