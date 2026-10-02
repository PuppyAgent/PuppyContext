import type { NextConfig } from 'next';
import { readFileSync, existsSync } from 'fs';
import { join } from 'path';
import createNextIntlPlugin from 'next-intl/plugin';

const withNextIntl = createNextIntlPlugin('./i18n/request.ts');

function loadVersion(): string {
  const candidates = [
    join(import.meta.dirname, '..', 'version.json'),
    join(import.meta.dirname, 'version.json'),
  ];
  for (const p of candidates) {
    if (existsSync(p)) {
      return JSON.parse(readFileSync(p, 'utf-8')).version;
    }
  }
  return process.env.APP_VERSION ?? '0.0.0';
}

const nextConfig: NextConfig = {
  // Run production verification without overwriting a live dev server's cache.
  distDir: process.env.PUPPYONE_NEXT_DIST_DIR || '.next',
  reactStrictMode: true,
  transpilePackages: ['@puppyone/cloud-core'],
  output: 'standalone',
  outputFileTracingRoot: import.meta.dirname,
  env: {
    NEXT_PUBLIC_APP_VERSION: loadVersion(),
  },
  experimental: {
    // Explicit local preview builds must not fan out across the workstation.
    cpus: process.env.PUPPYONE_LOCAL_BUILD === '1' ? 1 : undefined,
    optimizePackageImports: [
      'lucide-react',
      'framer-motion',
      'react-syntax-highlighter',
    ],
  },
  images: {
    formats: ['image/avif', 'image/webp'],
  },
  compiler: {
    removeConsole: process.env.NODE_ENV === 'production'
      ? { exclude: ['error', 'warn'] }
      : false,
  },
};

export default withNextIntl(nextConfig);
