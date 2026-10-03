'use client';

import { StatusDot as BaseStatusDot } from '@/components/ui/StatusDot';
import type { ProviderIconLookup } from '@/features/files/components/access-points/types';
import type { EntrypointBadge } from '@/features/files/components/explorer';
import {
  isMcpProvider,
  isSandboxProvider,
} from '@/lib/accessProviderRegistry';
import { resolveProviderIconUrl } from '@/lib/providerIcons';

export function StatusDot({ status, borderColor = 'var(--po-panel)' }: { status: string; borderColor?: string }) {
  return (
    <BaseStatusDot
      status={status}
      style={{
        position: 'absolute',
        right: -1,
        bottom: -1,
        border: `1px solid ${borderColor}`,
        boxSizing: 'border-box',
      }}
    />
  );
}

function McpMiniIcon() {
  return (
    <svg width={16} height={16} viewBox="0 0 24 24" fill="none" stroke="var(--po-accent)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="3" width="20" height="14" rx="2" />
      <line x1="8" y1="21" x2="16" y2="21" />
      <line x1="12" y1="17" x2="12" y2="21" />
    </svg>
  );
}

function SandboxMiniIcon() {
  return (
    <svg width={16} height={16} viewBox="0 0 24 24" fill="none" stroke="var(--po-warning)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="4 17 10 11 4 5" />
      <line x1="12" y1="19" x2="20" y2="19" />
    </svg>
  );
}

export function DefaultProviderIcon() {
  return (
    <svg width={16} height={16} viewBox="0 0 24 24" fill="none" stroke="var(--po-text-muted)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="18" height="18" rx="4" />
      <path d="M8 12h8" />
    </svg>
  );
}

function AgentMiniIcon() {
  return (
    <svg width={16} height={16} viewBox="0 0 24 24" fill="none" stroke="var(--po-file-accent-audio)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  );
}

export function AccessPointProviderIcon({
  ep,
  providerIcons,
}: {
  ep: Pick<EntrypointBadge, 'provider'>;
  providerIcons: ProviderIconLookup;
}) {
  if (ep.provider.startsWith('agent:')) return <AgentMiniIcon />;
  if (isMcpProvider(ep.provider)) return <McpMiniIcon />;
  if (isSandboxProvider(ep.provider)) return <SandboxMiniIcon />;
  const providerIcon = providerIcons[ep.provider];
  const iconUrl = resolveProviderIconUrl({
    provider: ep.provider,
    icon: providerIcon?.icon,
    iconUrl: providerIcon?.iconUrl,
  });
  if (iconUrl) {
    return <img src={iconUrl} alt="" width={16} height={16} style={{ display: 'block', borderRadius: 2 }} />;
  }

  return providerIcon?.icon ? <span style={{ fontSize: 14, lineHeight: 1 }}>{providerIcon.icon}</span> : <DefaultProviderIcon />;
}
