import { FlatCompat } from '@eslint/eslintrc';
import { fileURLToPath } from 'node:url';
import typescriptParser from '@typescript-eslint/parser';
import { createRequire } from 'node:module';

const compat = new FlatCompat({ baseDirectory: fileURLToPath(new URL('.', import.meta.url)) });
const require = createRequire(import.meta.url);

const config = [
  {
    ignores: [
      '**/node_modules/**', '**/.next*/**', '**/out/**', '**/dist/**',
      '**/coverage/**', '**/next-env.d.ts', 'artifacts/**',
    ],
  },
  ...compat.extends('next/core-web-vitals'),
  {
    // FlatCompat's legacy TS override is rooted in frontend; include root packages too.
    files: ['**/*.{ts,tsx,mts,cts}'],
    languageOptions: { parser: typescriptParser },
  },
  {
    files: ['**/*.{js,jsx,mjs,cjs,ts,tsx,mts,cts}'],
    settings: { next: { rootDir: fileURLToPath(new URL('.', import.meta.url)) } },
    languageOptions: {
      parserOptions: { babelOptions: { presets: [require.resolve('next/babel')] } },
    },
    // Legacy next lint did not report unused eslint-disable comments.
    linterOptions: { reportUnusedDisableDirectives: false },
    rules: {
      complexity: ['error', 20],
      'max-depth': ['error', 4],
      'max-params': ['error', 5],
      'no-nested-ternary': 'error',
      'max-lines': ['error', { max: 800, skipBlankLines: true, skipComments: true }],
      'max-lines-per-function': ['error', { max: 300, skipBlankLines: true, skipComments: true }],
    },
  },
];

export default config;
