'use client';

import { useMemo } from 'react';
import { Dots } from '@/components/loading';
import {
  ACTIVITY_BG,
  ACTIVITY_BORDER,
  ACTIVITY_SHADOW,
  ACTIVITY_WIDTH,
  activityHeaderStyle,
  activityTitleStyle,
} from './activityStyles';
import type { ActivityItem } from '@/lib/activityApi';

type SyncJobsWidgetProps = {
  activeItems: readonly ActivityItem[];
  inline?: boolean;
};

/**
 * Transient widget for in-progress Connect/Sync runs.
 *
 * Read-only sibling of ImportJobsWidget: it consumes the unified activity
 * feed filtered to `synchronize_run`, so durable-binding runs surface in the
 * same Activity stack as uploads and imports. Sync runs are not cancellable
 * from here (no close button) — the feed is display-only.
 */
export function SyncJobsWidget({ activeItems, inline = false }: SyncJobsWidgetProps) {
  const runs = useMemo(() => activeItems.slice(0, 3), [activeItems]);

  if (activeItems.length === 0) return null;

  const title = activeItems.length === 1 ? 'Syncing' : `${activeItems.length} syncs`;

  const containerStyle: React.CSSProperties = inline
    ? { position: 'relative', fontFamily: 'var(--po-font-sans)' }
    : {
        position: 'fixed',
        bottom: 20,
        right: 20,
        zIndex: 9999,
        fontFamily: 'var(--po-font-sans)',
      };

  return (
    <div style={containerStyle}>
      <div
        style={{
          width: ACTIVITY_WIDTH,
          background: ACTIVITY_BG,
          border: ACTIVITY_BORDER,
          borderRadius: 12,
          boxShadow: ACTIVITY_SHADOW,
          backdropFilter: 'blur(28px) saturate(160%)',
          WebkitBackdropFilter: 'blur(28px) saturate(160%)',
          overflow: 'hidden',
          color: 'var(--po-text)',
        }}
      >
        <div style={activityHeaderStyle}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
            <Dots size="xs" tone="info" ariaLabel="Syncing" />
            <span style={activityTitleStyle}>{title}</span>
          </div>
        </div>

        <div style={{ padding: '0 12px 12px' }}>
          {runs.map(run => (
            <div key={run.id} style={{ paddingTop: 9 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                <div
                  style={{
                    minWidth: 0,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                    fontSize: 12,
                    color: 'var(--po-text)',
                  }}
                  title={run.label || undefined}
                >
                  {run.label || 'Sync'}
                </div>
                <div
                  style={{
                    flexShrink: 0,
                    fontSize: 11,
                    color: 'var(--po-text-subtle)',
                    fontVariantNumeric: 'tabular-nums',
                  }}
                >
                  {Math.max(0, Math.min(100, run.progress ?? 0))}%
                </div>
              </div>
              <div
                style={{
                  marginTop: 6,
                  height: 3,
                  overflow: 'hidden',
                  borderRadius: 999,
                  background: 'var(--po-border-subtle)',
                }}
              >
                <div
                  style={{
                    width: `${Math.max(4, Math.min(100, run.progress || 8))}%`,
                    height: '100%',
                    borderRadius: 999,
                    background: 'var(--po-accent)',
                    transition: 'width 0.18s ease-out',
                  }}
                />
              </div>
              <div
                style={{
                  marginTop: 5,
                  fontSize: 11,
                  lineHeight: '15px',
                  color: 'var(--po-text-subtle)',
                }}
              >
                {run.message || run.phase || 'Syncing'}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
