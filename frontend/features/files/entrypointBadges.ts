import type { EntrypointBadge } from './DataLayoutContext';

export function entrypointBadgeKey(badge: Pick<EntrypointBadge, 'resourceKind' | 'id'>): string {
  return `${badge.resourceKind}:${badge.id}`;
}

/** Access must never win the binding selector, even on the same path or ID. */
export function selectSynchronizeBadges(entries: ReadonlyMap<string, readonly EntrypointBadge[]>) {
  const selected = new Map<string, Extract<EntrypointBadge, { resourceKind: 'synchronize' }>>();
  for (const [path, badges] of entries) {
    const binding = badges.find(badge => badge.resourceKind === 'synchronize');
    if (binding?.resourceKind === 'synchronize') selected.set(path, binding);
  }
  return selected;
}
