// @vitest-environment node
import { afterEach, expect, it, vi } from 'vitest';
import { GET, HEAD, dynamic, runtime } from '@/app/api/health/route';

afterEach(() => vi.unstubAllEnvs());
it('returns a typed, uncached liveness response without auth or dependency calls', async () => {
  vi.stubEnv('NEXT_PUBLIC_APP_VERSION', 'test-version');
  vi.stubEnv('NODE_ENV', 'production');
  const fetcher = vi.spyOn(globalThis, 'fetch');
  const response = GET();
  expect(await response.json()).toEqual({ schemaVersion: 1, service: 'puppyone-cloud-web', status: 'ok', mode: 'production', version: 'test-version' });
  expect(response.status).toBe(200);
  expect(response.headers.get('cache-control')).toBe('no-store');
  expect(response.headers.get('set-cookie')).toBeNull();
  expect(fetcher).not.toHaveBeenCalled();
  expect(dynamic).toBe('force-dynamic');
  expect(runtime).toBe('nodejs');
});
it('supports body-free HEAD and reports no credentials or infrastructure details', async () => {
  const response = HEAD();
  expect(await response.text()).toBe('');
  expect(response.headers.get('x-puppyone-service')).toBe('puppyone-cloud-web');
  expect(response.headers.get('cache-control')).toBe('no-store');
});
