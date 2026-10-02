// Liveness only: no page render, auth/session refresh, or dependency probes.
// The /api prefix is deliberately outside the authentication middleware.
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const headers = { 'Cache-Control': 'no-store', 'X-Puppyone-Service': 'puppyone-cloud-web' };

export function GET() {
  return Response.json({
    schemaVersion: 1,
    service: 'puppyone-cloud-web',
    status: 'ok',
    mode: process.env.NODE_ENV === 'production' ? 'production' : 'development',
    version: process.env.NEXT_PUBLIC_APP_VERSION ?? '0.0.0',
  }, { headers });
}

export function HEAD() {
  return new Response(null, { status: 200, headers });
}
