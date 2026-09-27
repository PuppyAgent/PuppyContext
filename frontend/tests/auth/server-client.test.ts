// @vitest-environment node
import { afterEach, expect, it, vi } from 'vitest';
import { createBrowserClient } from '@supabase/ssr';
import { createAuthServerClient } from '@/features/auth/supabase/server-client';

afterEach(() => vi.unstubAllEnvs());

it.each([undefined, 'http://kong:8000'])(
  'reads browser session cookies when server transport is %s',
  async internal => {
    const publicUrl = 'https://project.example.test';
    vi.stubEnv('NEXT_PUBLIC_SUPABASE_URL', publicUrl);
    vi.stubEnv('NEXT_PUBLIC_SUPABASE_ANON_KEY', 'public-test-key');
    vi.stubEnv('SUPABASE_INTERNAL_URL', internal);
    const jar = new Map<string, string>();
    const cookies = {
      getAll: () => [...jar].map(([name, value]) => ({ name, value })),
      setAll: (values: { name: string; value: string }[]) => {
        values.forEach(({ name, value }) => value ? jar.set(name, value) : jar.delete(name));
      },
    };
    const user = { id: '00000000-0000-4000-8000-000000000001', email: 'test@example.com' };
    const payload = Buffer.from(JSON.stringify({ sub: user.id, exp: Math.floor(Date.now() / 1000) + 3600 })).toString('base64url');
    const token = `eyJhbGciOiJIUzI1NiJ9.${payload}.synthetic-signature`;
    const requests: Request[] = [];
    const provider: typeof fetch = async (input, init) => {
      const request = new Request(input, init);
      requests.push(request);
      expect(request.headers.get('authorization')).toBe(`Bearer ${token}`);
      return Response.json(user);
    };
    // Use the actual SDK cookie writer/reader, not a hand-written cookie name.
    const browser = createBrowserClient(publicUrl, 'public-test-key', {
      cookies, isSingleton: false, global: { fetch: provider },
    });
    expect((await browser.auth.setSession({ access_token: token, refresh_token: 'refresh' })).error).toBeNull();
    expect(jar.size).toBeGreaterThan(0);
    const server = createAuthServerClient({ cookies, global: { fetch: provider } });
    const result = await server.auth.getUser();
    expect(result.error).toBeNull();
    expect(result.data.user?.id).toBe(user.id);
    expect(requests[0].url).toBe(`${publicUrl}/auth/v1/user`);
    expect(requests.at(-1)?.url).toBe(`${internal ?? publicUrl}/auth/v1/user`);
    await browser.auth.stopAutoRefresh();
    await server.auth.stopAutoRefresh();
  },
);
