import { NextResponse, type NextRequest } from 'next/server';
import { createAuthServerClient } from '@/features/auth/supabase/server-client';
import { safeNext } from '@/features/auth/login-intent';

const LOCALES = new Set(['en', 'zh-CN']);
const PROTECTED_ROUTES = [
  '/projects',
  '/tools',
  '/connect',
  '/mcp',
  '/etl',
  '/home',
  '/settings',
  '/billing',
  '/team',
  '/tools-and-server',
];

function detectLocale(request: NextRequest): string {
  const cookie = request.cookies.get('NEXT_LOCALE')?.value;
  if (cookie && LOCALES.has(cookie)) return cookie;
  return request.headers.get('accept-language')?.toLowerCase().includes('zh') ? 'zh-CN' : 'en';
}

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  let response = NextResponse.next({ request: { headers: request.headers } });
  const finish = (destination?: URL) => {
    if (destination) {
      const redirected = NextResponse.redirect(destination);
      // Preserve all refreshed cookie chunks on redirects as well as renders.
      response.cookies.getAll().forEach(cookie => redirected.cookies.set(cookie));
      response = redirected;
    }
    response.headers.set('x-next-intl-locale', detectLocale(request));
    response.headers.set('Cache-Control', 'private, no-store');
    if (pathname === '/login') response.headers.set('Referrer-Policy', 'no-referrer');
    return response;
  };

  // The native flow owns a temporary, independent browser session. Reading or
  // refreshing Web cookies here would silently mix two login contexts.
  if (pathname === '/login' && request.nextUrl.searchParams.get('client') === 'desktop')
    return finish();

  const supabase = createAuthServerClient({
    cookies: {
      getAll: () => request.cookies.getAll(),
      setAll: values => {
        values.forEach(({ name, value }) => request.cookies.set(name, value));
        const previous = response.cookies.getAll();
        response = NextResponse.next({ request: { headers: request.headers } });
        previous.forEach(cookie => response.cookies.set(cookie));
        values.forEach(({ name, value, options }) => response.cookies.set(name, value, options));
      },
    },
  });
  // getSession only reads storage. Verify identity with Supabase before using it
  // for a route decision; the API still performs its own JWT/permission checks.
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (pathname === '/') return finish(new URL('/home', request.url));
  if (user && pathname === '/login') {
    return finish(
      new URL(safeNext(request.nextUrl.searchParams.get('redirect')) ?? '/home', request.url)
    );
  }
  const protectedRoute = PROTECTED_ROUTES.some(
    route => pathname === route || pathname.startsWith(route + '/')
  );
  if (!user && protectedRoute) {
    const login = new URL('/login', request.url);
    login.searchParams.set('redirect', pathname + request.nextUrl.search);
    return finish(login);
  }
  return finish();
}

// Auth callbacks own their cookie exchange and are deliberately outside this middleware.
export const config = {
  matcher: [
    '/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$|auth/callback|auth/confirm|oauth/callback|api).*)',
  ],
};
