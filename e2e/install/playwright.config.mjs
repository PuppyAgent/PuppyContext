import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: 'installation.spec.mjs',
  workers: 1,
  retries: 0,
  timeout: 120_000,
  reporter: [['list'], ['json', { outputFile: process.env.INSTALL_REPORT }]],
  use: { headless: true },
});
