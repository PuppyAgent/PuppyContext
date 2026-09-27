import { createServerClient } from '@supabase/ssr';
import { getPublicSupabaseUrl, getServerSupabaseUrl, getSupabaseAnonKey } from '@/lib/server-env';

type ServerOptions = Parameters<typeof createServerClient>[2];

/** Keep Web cookie/PKCE identity public; route server I/O through private DNS. */
export function createAuthServerClient(options: ServerOptions) {
  const publicUrl = getPublicSupabaseUrl();
  const internalUrl = getServerSupabaseUrl();
  const publicBase = new URL(publicUrl);
  const publicPath = publicBase.pathname.replace(/\/$/, '');
  const fetcher = options.global?.fetch ?? fetch;
  const transport: typeof fetch = (input, init) => {
    const url = new URL(input instanceof Request ? input.url : input);
    if (
      internalUrl !== publicUrl && url.origin === publicBase.origin &&
      (url.pathname === publicPath || url.pathname.startsWith(publicPath + '/'))
    ) {
      const target = new URL(internalUrl);
      target.pathname = target.pathname.replace(/\/$/, '') + url.pathname.slice(publicPath.length);
      target.search = url.search;
      return fetcher(input instanceof Request ? new Request(target, input) : target, init);
    }
    return fetcher(input, init);
  };
  // Supabase derives cookie names from this URL. Passing the internal hostname
  // here creates a different session namespace from the browser's client.
  return createServerClient(publicUrl, getSupabaseAnonKey(), {
    ...options,
    global: { ...options.global, fetch: transport },
  });
}
