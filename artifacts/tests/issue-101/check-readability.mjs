// Temporary ISSUE-101 acceptance check; the repository lint gate is owned by ISSUE-105.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { Linter } from '../../../frontend/node_modules/eslint/lib/api.js';
import parser from '../../../frontend/node_modules/@typescript-eslint/parser/dist/index.js';
import ts from '../../../frontend/node_modules/typescript/lib/typescript.js';

const root = new URL('../../../', import.meta.url);
const explorerPath = 'frontend/features/files/components/explorer/ExplorerTreeRow.tsx';
const explorer = readFileSync(new URL(explorerPath, root), 'utf8');
const ast = ts.createSourceFile(explorerPath, explorer, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const iconStatements = ast.statements.filter(statement => {
  if (ts.isFunctionDeclaration(statement)) return statement.name?.text === 'FileTypeIcon';
  return ts.isVariableStatement(statement) && statement.declarationList.declarations.some(
    declaration => declaration.name.getText(ast) === 'FILE_KIND_GLYPHS',
  );
});
if (!iconStatements.length) throw new Error('FileTypeIcon audit target is missing');
const targets = [
  { name: 'FileTypeIcon and lookup table', source: iconStatements.map(statement => statement.getText(ast)).join('\n') },
  { name: 'paneLayout.ts (all functions including extracted helpers)', source: readFileSync(new URL('frontend/features/workspace/paneLayout.ts', root), 'utf8') },
];
const linter = new Linter();
function lint(source, rules) {
  return linter.verify(source, [{
    files: ['**/*.tsx'],
    languageOptions: { parser, parserOptions: { ecmaFeatures: { jsx: true } } },
    rules,
  }], { filename: fileURLToPath(new URL('./acceptance.tsx', import.meta.url)), allowInlineConfig: false });
}
let failures = 0;
for (const target of targets) {
  const violations = lint(target.source, {
    complexity: ['error', 20],
    'max-depth': ['error', 4],
    'no-nested-ternary': 'error',
    'max-lines-per-function': ['error', { max: 300, skipBlankLines: true, skipComments: true }],
  });
  const metrics = lint(target.source, {
    complexity: ['warn', 0],
    'max-lines-per-function': ['warn', { max: 0, skipBlankLines: true, skipComments: true }],
  }).map(({ line, message }) => ({ line, message }));
  failures += violations.length;
  console.log(JSON.stringify({ target: target.name, violations, metrics }, null, 2));
}
process.exitCode = failures ? 1 : 0;
