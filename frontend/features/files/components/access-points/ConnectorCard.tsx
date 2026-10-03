'use client';

import { AccessPointProviderIcon, StatusDot } from '@/features/files/components/access-points/AccessPointProviderIcon';
import {
  accessSurfaceIcon,
  directionLabel,
  providerLabel,
} from '@/features/files/components/access-points/labels';
import {
  COLOR_BG_CARD,
  COLOR_BG_HOVER,
  COLOR_BORDER,
  COLOR_BORDER_HOVER,
  COLOR_FG,
  COLOR_FG_DIM,
} from '@/features/files/components/access-points/tokens';
import type { ProviderIconLookup } from '@/features/files/components/access-points/types';
import type { AccessSurface } from '@/lib/repoApi';
import { useMemo, useState } from 'react';

/**
 * ConnectorCard — third-party Connect row rendered in the access-point UI.
 *
 * cli + agent built-ins are NOT rendered here; ConnectMethodsBlock owns
 * those. This card just shows the provider icon, name, direction, status,
 * and bubbles the click up to the parent for routing into the
 * sync_config detail panel.
 */
export function ConnectorCard({
  connector,
  providerIcons,
  onClick,
  onHoverEnter,
  onHoverLeave,
}: {
  readonly connector: AccessSurface;
  readonly providerIcons: ProviderIconLookup;
  readonly onClick: () => void;
  readonly onHoverEnter?: () => void;
  readonly onHoverLeave?: () => void;
}) {
  const [hovered, setHovered] = useState(false);
  const iconEp = useMemo(() => accessSurfaceIcon(connector), [connector]);
  const displayName = connector.name || providerLabel(connector.kind);

  return (
    <button
      type="button"
      onClick={onClick}
      onMouseEnter={() => { setHovered(true); onHoverEnter?.(); }}
      onMouseLeave={() => { setHovered(false); onHoverLeave?.(); }}
      style={{
        width: '100%',
        textAlign: 'left',
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        minHeight: 54,
        padding: '10px 12px',
        borderRadius: 8,
        border: `1px solid ${hovered ? COLOR_BORDER_HOVER : COLOR_BORDER}`,
        background: hovered ? COLOR_BG_HOVER : COLOR_BG_CARD,
        color: COLOR_FG,
        cursor: 'pointer',
        transition: 'all 0.15s',
      }}
    >
      <AccessPointProviderIcon ep={iconEp} providerIcons={providerIcons} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 12, fontWeight: 500, color: COLOR_FG, lineHeight: 1.3 }}>
          {displayName}
        </div>
        <div style={{ fontSize: 10, color: COLOR_FG_DIM, lineHeight: 1.4, marginTop: 2 }}>
          {providerLabel(connector.kind)} · {directionLabel(connector.direction ?? '')}
        </div>
      </div>
      <StatusDot status={connector.status} />
    </button>
  );
}
