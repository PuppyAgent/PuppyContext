import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { safeNext, readLoginIntent } from '@/features/auth/login-intent';
import { createLoginCoordinator } from '@/features/auth/login-coordinator';
import { completeDesktopHandoff } from '@/features/auth/desktop-handoff';

const mocks = vi.hoisted(() => ({ server: vi.fn(), exchange: vi.fn(), setCookie: vi.fn() }));
vi.mock('@supabase/ssr', () => ({ createServerClient: mocks.server }));
vi.mock('next/headers', () => ({
  cookies: async () => ({ getAll: () => [], set: mocks.setCookie }),
}));
vi.mock('@/lib/server-env', () => ({
  getRequestOrigin: () => 'https://web.example.test',
  getServerSupabaseUrl: () => 'https://auth.example.test',
  getPublicSupabaseUrl: () => 'https://auth.example.test',
  getSupabaseAnonKey: () => 'public-key',
  getServerApiBaseUrl: () => 'https://api.example.test',
}));
import { GET } from '@/app/auth/callback/route';
import { middleware } from '@/middleware';
import { NextRequest } from 'next/server';
const state = 'a'.repeat(43);
const session = { access_token: 'access', refresh_token: 'refresh', user: { id: 'user' } } as any;
beforeEach(() => {
  mocks.server.mockReturnValue({ auth: { exchangeCodeForSession: mocks.exchange } });
});
afterEach(() => vi.unstubAllGlobals());

it.each([
  '//evil.test',
  '/\\evil.test',
  '/%5cevil.test',
  '/login',
  '/auth/callback',
  '/\nevil.test',
])('rejects unsafe return path %s', value => expect(safeNext(value)).toBeNull());
it('preserves valid app return path and rejects incomplete Desktop intent', () => {
  expect(safeNext('/invite/abc?x=1')).toBe('/invite/abc?x=1');
  expect(() => readLoginIntent(new URL('https://web.test/login?client=desktop'))).toThrow();
});

it('Desktop OAuth callback preserves intent without using Web cookie session', async () => {
  const response = await GET(
    new Request(`https://web.example.test/auth/callback?desktop_state=${state}&code=provider-code`)
  );
  const target = new URL(response.headers.get('location')!);
  expect(target.pathname).toBe('/login');
  expect(target.searchParams.get('auth_code')).toBe('provider-code');
  expect(target.searchParams.get('desktop_state')).toBe(state);
  expect(mocks.server).not.toHaveBeenCalled();
  expect(response.headers.get('cache-control')).toContain('no-store');
  expect(response.headers.get('referrer-policy')).toBe('no-referrer');
});

it('provider cancellation preserves the Desktop attempt', async () => {
  const response = await GET(
    new Request(
      `https://web.example.test/auth/callback?desktop_state=${state}&error=access_denied&error_description=private`
    )
  );
  const target = new URL(response.headers.get('location')!);
  expect(target.searchParams.get('desktop_state')).toBe(state);
  expect(target.searchParams.get('error')).toBe('auth_cancelled');
  expect(target.search).not.toContain('private');
});

it('Desktop login middleware never touches the existing Web session', async () => {
  const response = await middleware(
    new NextRequest(`https://web.example.test/login?client=desktop&desktop_state=${state}`, {
      headers: { cookie: 'web-session=existing' },
    })
  );
  expect(response.status).toBe(200);
  expect(mocks.server).not.toHaveBeenCalled();
  expect(response.headers.get('set-cookie')).toBeNull();
});

it('Web OAuth callback exchanges once, initializes and returns to invite', async () => {
  mocks.exchange.mockResolvedValue({ data: { session }, error: null });
  const fetcher = vi.fn(async () => Response.json({ data: { demo_project_id: 'demo' } }));
  vi.stubGlobal('fetch', fetcher);
  const response = await GET(
    new Request('https://web.example.test/auth/callback?code=code&next=%2Finvite%2Fabc')
  );
  expect(response.headers.get('location')).toBe('https://web.example.test/invite/abc');
  expect(mocks.exchange).toHaveBeenCalledOnce();
  expect(fetcher.mock.calls).toHaveLength(1);
});

it('parallel completion shares one exchange and clears only after success', async () => {
  const clear = vi.fn();
  const fetcher = vi.fn(async () =>
    Response.json({
      data: { redirect_url: `http://127.0.0.1:4000/auth/callback?state=${state}&code=code` },
    })
  );
  const coordinator = createLoginCoordinator({
    client: { auth: { getSession: async () => ({ data: { session } }) } } as any,
    intent: { kind: 'desktop', state },
    proof: 'proof',
    clear,
    fetcher,
  });
  const results = await Promise.all([
    coordinator.finish(),
    coordinator.finish(),
    coordinator.finish(),
  ]);
  expect(new Set(results).size).toBe(1);
  expect(fetcher).toHaveBeenCalledOnce();
  expect(clear).toHaveBeenCalledOnce();
});

it.each([
  'https://evil.test/?code=x',
  'http://127.0.0.1:4000/wrong?code=x',
  `http://127.0.0.1:4000/auth/callback?code=x&state=other`,
])('rejects unexpected handoff destination %s', async redirect => {
  const fetcher = vi.fn(async () => Response.json({ data: { redirect_url: redirect } }));
  await expect(completeDesktopHandoff(state, 'proof', session, fetcher)).rejects.toThrow(
    'invalid callback'
  );
});
