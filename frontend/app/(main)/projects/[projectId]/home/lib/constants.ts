// Provider-related constants used across Home subcomponents.

import { ACCESS_PROVIDER_LABELS } from '@/lib/accessProviderRegistry';
import type { ApDirection, DashboardEntrypoint } from './types';

export const PROVIDER_LABELS = ACCESS_PROVIDER_LABELS;

export const PROVIDER_COLORS: Record<string, string> = {
  agent: 'var(--po-file-accent-audio)', mcp: 'var(--po-accent)', sandbox: 'var(--po-warning)',
  git_remote: 'var(--po-success)', cli: 'var(--po-accent)',
  gmail: 'var(--po-danger)', github: 'var(--po-text)', google_sheets: 'var(--po-success)', google_docs: 'var(--po-accent)',
  notion: 'var(--po-text)', supabase: 'var(--po-success)', url: 'var(--po-text-subtle)',
};

/** Missing direction is unknown, never inferred from Provider classification. */
export function getApDirection(conn: Pick<DashboardEntrypoint, 'direction'>): ApDirection | null {
  const d = conn.direction;
  return d === 'inbound' || d === 'outbound' || d === 'bidirectional' ? d : null;
}

// Numeric agent icons get mapped to one of these emoji at render time so
// the avatar never shows a bare number. Index = `parseInt(icon) % len`.
export const AGENT_ICONS = [
  '🐗', '🐙', '🐷', '🦄', '🐧', '🦉', '🐼', '🐝', '🐸', '🐱',
];
