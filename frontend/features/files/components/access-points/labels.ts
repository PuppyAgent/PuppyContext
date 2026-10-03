import type { EntrypointBadge } from '@/features/files/components/explorer';
import {
  getAccessProviderLabel,
  isAgentProvider,
  normalizeConnectorProvider,
} from '@/lib/accessProviderRegistry';
import type { AccessSurface, RepositoryView } from '@/lib/repoApi';

export function providerLabel(provider: string): string {
  return getAccessProviderLabel(provider);
}

export function directionLabel(direction: string): string {
  if (direction === 'bidirectional') return 'Two-way';
  if (direction === 'inbound') return 'Import';
  if (direction === 'outbound') return 'Export';
  return direction || '—';
}

/**
 * Compact header meta line: `path · mode · excludes`. Empty path gets the
 * canonical `/` so the user can see at a glance whether they're at the
 * Project root or a Scope subtree.
 */
export function buildScopeMetaLine(scope: RepositoryView): string {
  const parts: string[] = [];
  parts.push(scope.path === '' ? '/' : `/${scope.path}`);
  parts.push(scope.max_mode === 'rw' ? 'Read & Write' : 'Read-only');
  if (scope.exclude && scope.exclude.length > 0) {
    parts.push(
      `${scope.exclude.length} exclude${scope.exclude.length === 1 ? '' : 's'}`,
    );
  }
  if (scope.target.kind === 'project_root') parts.push('project root');
  return parts.join(' · ');
}

/** Icons need display metadata, never a fabricated binding identity. */
export function accessSurfaceIcon(c: AccessSurface): Pick<EntrypointBadge, 'provider'> {
  let iconProvider: string;
  if (isAgentProvider(c.kind)) iconProvider = 'agent:chat';
  else iconProvider = normalizeConnectorProvider(c.kind);
  return { provider: iconProvider };
}

export function getApiBase(): string {
  if (globalThis.window === undefined) return process.env.NEXT_PUBLIC_API_URL || '';
  return process.env.NEXT_PUBLIC_API_URL || globalThis.location.origin;
}
