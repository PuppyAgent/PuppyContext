'use client';

import React, { useState, useCallback, useEffect, useRef } from 'react';
import useSWR from 'swr';
import {
  getSynchronizeStatus, refreshSynchronizeBinding, pauseSynchronizeBinding,
  resumeSynchronizeBinding, deleteSynchronizeBinding, updateSynchronizeTrigger,
  type SynchronizeStatus, type SynchronizeStatusItem,
} from '@/lib/synchronizeApi';
import { SYNC_MODE_META, getProviderDisplayLabel, getSyncTriggerPolicy } from '@/lib/syncTriggerPolicy';
import type { SyncModeType } from '@/lib/syncTriggerPolicy';
import { useConnectorSpecs } from '@/lib/hooks/useData';
import { PanelShell } from '@/features/files/components/PanelShell';
import { Dots } from '@/components/loading';
import { ActivityIconButton } from '@/components/ActivityIconButton';
import { StatusIndicator } from '@/components/ui/StatusDot';

type SyncDetail = SynchronizeStatusItem;

// Mini DocShell — matches the product's document icon with folded corner
function MiniDocShell({ type }: { type: 'json' | 'markdown' | 'file' }) {
  const accentColor = type === 'json' ? 'var(--po-success)' : type === 'markdown' ? 'var(--po-accent)' : 'var(--po-text-muted)';
  const label = type === 'json' ? '{ }' : type === 'markdown' ? 'MD' : 'FILE';
  return (
    <svg width="36" height="40" viewBox="0 0 36 40" fill="none">
      <path d="M4 2C4 1.44772 4.44772 1 5 1H23L32 10V38C32 38.5523 31.5523 39 31 39H5C4.44772 39 4 38.5523 4 38V2Z"
        fill="var(--po-file-icon-body)" stroke="var(--po-file-icon-stroke)" strokeWidth="0.75" />
      <path d="M23 1V10H32" stroke="var(--po-file-icon-stroke)" strokeWidth="0.75" strokeLinejoin="round" />
      <path d="M23 1V10H32L23 1Z" fill="var(--po-file-icon-fold)" />
      <text x="18" y="28" textAnchor="middle" fontSize="7" fontWeight="600" fill={accentColor} fontFamily="var(--po-font-sans)">
        {label}
      </text>
    </svg>
  );
}

interface SyncDetailViewProps {
  syncId: string;
  projectId?: number | string;
  onClose?: () => void;
  onBack?: () => void;
}

// ============================================================
// Provider logos — reuse /icons/* assets from public folder
// ============================================================

function ProviderImg({ src, alt, size }: { src: string; alt: string; size: number }) {
  return <img src={src} alt={alt} width={size} height={size} style={{ display: 'block' }} />;
}

function GitHubLogo({ size = 24 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="var(--po-text-inverse)">
      <path fillRule="evenodd" clipRule="evenodd" d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/>
    </svg>
  );
}

function getProviderLogo(provider: string, size: number) {
  switch (provider) {
    case 'gmail': return <ProviderImg src="/icons/gmail.svg" alt="Gmail" size={size} />;
    case 'google_calendar': return <ProviderImg src="/icons/google_calendar.svg" alt="Google Calendar" size={size} />;
    case 'google_sheets': return <ProviderImg src="/icons/google_sheet.svg" alt="Google Sheets" size={size} />;
    case 'google_drive': return <ProviderImg src="/icons/google_doc.svg" alt="Google Drive" size={size} />;
    case 'google_docs': return <ProviderImg src="/icons/google_doc.svg" alt="Google Docs" size={size} />;
    case 'github': return <GitHubLogo size={size} />;
    case 'notion': return <ProviderImg src="/icons/notion.svg" alt="Notion" size={size} />;
    case 'linear': return <ProviderImg src="/icons/linear.svg" alt="Linear" size={size} />;
    default:
      return (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
          <rect x="2" y="2" width="20" height="20" rx="4" fill="var(--po-border-strong)"/>
          <text x="12" y="16" textAnchor="middle" fontSize="12" fontWeight="700" fill="var(--po-text-muted)" fontFamily="var(--po-font-sans)">
            {provider.charAt(0).toUpperCase()}
          </text>
        </svg>
      );
  }
}

// ============================================================
// ConnectionLine — directional arrows
// ============================================================

function ConnectionLine({ direction, isActive, status }: { direction: string; color?: string; isActive: boolean; status: string }) {
  if (!isActive) {
    const lineColor = status === 'error' ? 'var(--po-danger)' : status === 'paused' ? 'var(--po-warning)' : 'var(--po-text-disabled)';
    return (
      <svg width="80" height="16" viewBox="0 0 80 16" fill="none" stroke={lineColor} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" strokeDasharray="4 4">
        <path d="M2 8h76" />
      </svg>
    );
  }

  const arrowColor = 'var(--po-success)';

  return direction === 'bidirectional' ? (
    <svg width="80" height="16" viewBox="0 0 80 16" fill="none" stroke={arrowColor} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 5h76M6 2L2 5l4 3M74 8l4 3-4 3M2 11h76" />
    </svg>
  ) : direction === 'outbound' ? (
    <svg width="80" height="16" viewBox="0 0 80 16" fill="none" stroke={arrowColor} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 8h76M6 4L2 8l4 4" />
    </svg>
  ) : (
    <svg width="80" height="16" viewBox="0 0 80 16" fill="none" stroke={arrowColor} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 8h76M74 4l4 4-4 4" />
    </svg>
  );
}

// ============================================================
// Constants
// ============================================================

const PROVIDER_LABELS: Record<string, string> = {
  github: 'GitHub', notion: 'Notion', gmail: 'Gmail',
  google_calendar: 'Google Calendar', google_sheets: 'Google Sheets',
  google_drive: 'Google Drive', google_docs: 'Google Docs',
  airtable: 'Airtable', linear: 'Linear',
  folder_access: 'Folder Access', folder_source: 'Folder Source',
  url: 'URL Import', webhook: 'Webhook',
};

const DIRECTION_LABELS: Record<string, string> = {
  inbound: 'Syncing to Puppyone',
  outbound: 'Syncing from Puppyone',
  bidirectional: 'Bidirectional sync',
};

function relativeTime(iso: string | null | undefined): string {
  if (!iso) return '';
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

// ============================================================
// SyncDetailView
// ============================================================

export { getProviderLogo, PROVIDER_LABELS };

export function SyncDetailView({ syncId, projectId, onClose, onBack }: SyncDetailViewProps) {
  const [refreshing, setRefreshing] = useState(false);
  const { specs } = useConnectorSpecs();

  const { data: syncData, mutate } = useSWR<SynchronizeStatus>(
    projectId ? ['synchronize-status', String(projectId)] : null,
    () => getSynchronizeStatus(String(projectId)),
    { revalidateOnFocus: true },
  );

  const sync = syncData?.bindings?.find(s => s.id === syncId);

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    await mutate();
    setTimeout(() => setRefreshing(false), 600);
  }, [mutate]);

  const handleSyncRefresh = useCallback(async () => {
    if (!syncId) return;
    setRefreshing(true);
    try {
      await refreshSynchronizeBinding(syncId);
      await mutate();
    } catch (err) {
      console.error('Sync refresh failed:', err);
    } finally {
      setTimeout(() => setRefreshing(false), 1000);
    }
  }, [syncId, mutate]);

  const handlePause = useCallback(async () => {
    if (!syncId) return;
    try {
      await pauseSynchronizeBinding(syncId);
      await mutate();
    } catch (err) {
      console.error('Pause failed:', err);
    }
  }, [syncId, mutate]);

  const handleResume = useCallback(async () => {
    if (!syncId) return;
    try {
      await resumeSynchronizeBinding(syncId);
      await mutate();
    } catch (err) {
      console.error('Resume failed:', err);
    }
  }, [syncId, mutate]);

  const [disconnecting, setDisconnecting] = useState(false);
  const handleDisconnect = useCallback(async () => {
    if (!syncId || disconnecting) return;
    setDisconnecting(true);
    try {
      await deleteSynchronizeBinding(syncId);
      await mutate();
      onClose?.();
    } catch (err) {
      console.error('Disconnect failed:', err);
    } finally {
      setDisconnecting(false);
    }
  }, [syncId, disconnecting, mutate, onClose]);

  if (!sync) {
    return (
      <PanelShell title="Access" onClose={onClose || (() => {})} onBack={onBack}>
        <div style={{ display: 'flex', flex: 1, alignItems: 'center', justifyContent: 'center', color: 'var(--po-text-disabled)', fontSize: 12, height: '100%' }}>
          Sync endpoint not found
        </div>
      </PanelShell>
    );
  }

  const providerLabel = getProviderDisplayLabel(sync.provider, specs) !== sync.provider
    ? getProviderDisplayLabel(sync.provider, specs)
    : (PROVIDER_LABELS[sync.provider] || sync.provider);
  const dirLabel = DIRECTION_LABELS[sync.direction] || sync.direction;
  const isActive = sync.status === 'active' || sync.status === 'syncing';
  const isError = sync.status === 'error';
  const isPaused = sync.status === 'paused';

  const statusLabel = isError ? 'Error' : sync.status === 'syncing' ? 'Syncing' : isActive ? 'Sync active' : isPaused ? 'Paused' : sync.status || 'Inactive';
  const statusForIndicator = isError ? 'error' : sync.status === 'syncing' ? 'syncing' : isActive ? 'active' : isPaused ? 'paused' : 'inactive';

  const normalizedMode = normalizeMode(sync.trigger?.type);

  return (
    <>
      <PanelShell
        title={providerLabel}
        subtitle={sync.node_name || undefined}
        icon={getProviderLogo(sync.provider, 14)}
        onClose={onClose || (() => {})}
        onBack={onBack}
      >
        <div style={{ padding: '20px 24px 40px', display: 'flex', flexDirection: 'column', gap: 20 }}>

          {/* Sync visualization */}
          <div style={{
            borderRadius: 10, padding: '16px 0 8px',
            display: 'flex', flexDirection: 'column', gap: 6,
          }}>
            {/* Source (LEFT) → Arrow → Workspace (RIGHT) */}
            <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'center', gap: 0 }}>
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6, width: 72, flexShrink: 0 }}>
                <div style={{
                  width: 48, height: 48, borderRadius: 12,
                  background: 'var(--po-panel-raised)', border: '1px solid var(--po-border)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  boxShadow: '0 4px 12px var(--po-shadow)',
                }}>
                  {getProviderLogo(sync.provider, 24)}
                </div>
                <div style={{ fontSize: 11, fontWeight: 500, color: 'var(--po-text)', textAlign: 'center', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 72 }}>
                  {providerLabel}
                </div>
              </div>

              <div style={{
                display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
                width: 80, flexShrink: 0, paddingTop: 16,
              }}>
                <ConnectionLine
                  direction={sync.direction}
                  isActive={isActive}
                  status={sync.status}
                />
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6, width: 72, flexShrink: 0 }}>
                <div style={{
                  width: 48, height: 48, borderRadius: 12,
                  background: 'var(--po-panel-raised)', border: '1px solid var(--po-border)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  boxShadow: '0 4px 12px var(--po-shadow)',
                }}>
                  {sync.node_type === 'folder' || !sync.node_type
                    ? <img src="/icons/folder.svg" alt="Folder" width={24} height={24} style={{ display: 'block' }} />
                    : <div style={{ transform: 'scale(0.6)' }}><MiniDocShell type={sync.node_type as 'json' | 'markdown' | 'file'} /></div>
                  }
                </div>
                <div style={{ fontSize: 11, fontWeight: 500, color: 'var(--po-text)', textAlign: 'center', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 72 }}>
                  {sync.node_name || 'Workspace'}
                </div>
              </div>
            </div>

            {/* Status line */}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 4, paddingTop: 2 }}>
              <StatusIndicator status={statusForIndicator} label={statusLabel} style={{ fontSize: 11 }} />
              {sync.last_synced_at && (
                <span style={{ fontSize: 11, color: 'var(--po-text-disabled)' }}>
                  · {relativeTime(sync.last_synced_at)}
                </span>
              )}
              <button
                onClick={handleRefresh}
                title="Refresh"
                style={{
                  background: 'transparent', border: 'none', cursor: 'pointer',
                  color: 'var(--po-text-disabled)', width: 30, height: 30, padding: 0, borderRadius: 4, display: 'flex',
                  alignItems: 'center', justifyContent: 'center',
                  marginLeft: 'auto', transition: 'color 0.15s',
                }}
                onMouseEnter={e => e.currentTarget.style.color = 'var(--po-text-muted)'}
                onMouseLeave={e => e.currentTarget.style.color = 'var(--po-text-disabled)'}
              >
                {refreshing ? (
                  <Dots size="xs" ariaLabel="Refreshing" />
                ) : (
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
                  >
                    <polyline points="23 4 23 10 17 10" /><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
                  </svg>
                )}
              </button>
            </div>
          </div>

          {/* Error banner */}
          {sync.error_message && (
            <div style={{
              padding: '8px 12px', borderRadius: 6,
              background: 'color-mix(in srgb, var(--po-danger) 6%, transparent)', border: '1px solid color-mix(in srgb, var(--po-danger) 12%, transparent)',
            }}>
              <div style={{ fontSize: 11, color: 'var(--po-danger)', lineHeight: 1.5 }}>{sync.error_message}</div>
            </div>
          )}

          {/* Trigger mode selector */}
          <TriggerModeSelector
            syncId={sync.id}
            provider={sync.provider}
            currentMode={normalizedMode}
            currentTrigger={sync.trigger}
            onUpdated={mutate}
          />

          {/* Actions */}
          <div style={{ display: 'flex', gap: 6 }}>
            {normalizedMode === 'manual' && (
              <ActionButton label={refreshing ? 'Refreshing...' : 'Refresh now'} icon="retry" onClick={handleSyncRefresh} />
            )}
            {normalizedMode === 'scheduled' && (
              <ActionButton label={refreshing ? 'Syncing...' : 'Sync now'} icon="retry" onClick={handleSyncRefresh} />
            )}
            {isActive && (
              <ActionButton label="Pause" icon="pause" onClick={handlePause} />
            )}
            {isPaused && (
              <ActionButton label="Resume" icon="play" onClick={handleResume} />
            )}
            {isError && (
              <ActionButton label="Retry" icon="retry" onClick={handleSyncRefresh} />
            )}
            <ActionButton label={disconnecting ? 'Removing...' : 'Disconnect'} icon="disconnect" variant="danger" onClick={handleDisconnect} />
          </div>

          {/* Details — minimal */}
          <div style={{ padding: '0 4px' }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--po-text-subtle)', textTransform: 'uppercase', marginBottom: 8, letterSpacing: '0.5px' }}>
              Details
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <DetailRow label="Sync ID" value={sync.id} mono />
            </div>
          </div>

        </div>
      </PanelShell>
    </>
  );
}

// ============================================================
// PanelHeader — consistent with PanelShell
// ============================================================

function PanelHeader({ title, icon, onClose }: { title: string; icon?: React.ReactNode; onClose?: () => void }) {
  return (
    <div style={{
      height: 40, minHeight: 40, display: 'flex', alignItems: 'center', gap: 8,
      padding: '0 12px', borderBottom: '1px solid var(--po-border-subtle)', flexShrink: 0,
    }}>
      {icon && <span style={{ display: 'flex', alignItems: 'center', flexShrink: 0 }}>{icon}</span>}
      <span style={{ flex: 1, fontSize: 13, fontWeight: 500, color: 'var(--po-text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {title}
      </span>
      {onClose && (
        <ActivityIconButton kind="close" title="Close panel" onClick={onClose} />
      )}
    </div>
  );
}

// ============================================================
// normalizeMode — map backend values to canonical SyncModeType
// ============================================================

function normalizeMode(raw?: string): SyncModeType {
  if (!raw) return 'manual';
  if (raw === 'cli_push' || raw === 'realtime') return 'manual';
  if (raw === 'cron' || raw === 'scheduled') return 'scheduled';
  if (raw === 'manual') return 'manual';
  return 'manual';
}

// ============================================================
// TriggerModeSelector
// ============================================================

function TriggerModeSelector({
  syncId,
  provider,
  currentMode,
  currentTrigger,
  onUpdated,
}: {
  syncId: string;
  provider: string;
  currentMode: SyncModeType;
  currentTrigger: SyncDetail['trigger'];
  onUpdated: () => void;
}) {
  const { specs } = useConnectorSpecs();
  const policy = getSyncTriggerPolicy(provider, specs);
  const [saving, setSaving] = useState(false);
  const [editMode, setEditMode] = useState(false);
  const [pendingMode, setPendingMode] = useState<SyncModeType>(currentMode);
  const [scheduleConfig, setScheduleConfig] = useState<{ schedule?: string; timezone?: string } | null>(
    currentTrigger?.schedule ? { schedule: currentTrigger.schedule, timezone: currentTrigger.timezone || 'Asia/Shanghai' } : null,
  );

  const isLocked = policy.supportedModes.length <= 1;

  useEffect(() => {
    setPendingMode(currentMode);
    setScheduleConfig(
      currentTrigger?.schedule ? { schedule: currentTrigger.schedule, timezone: currentTrigger.timezone || 'Asia/Shanghai' } : null,
    );
  }, [currentMode, currentTrigger]);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      const trigger: { type: string; schedule?: string; timezone?: string } = { type: pendingMode };
      if (pendingMode === 'scheduled' && scheduleConfig?.schedule) {
        trigger.schedule = scheduleConfig.schedule;
        trigger.timezone = scheduleConfig.timezone || 'Asia/Shanghai';
      }
      await updateSynchronizeTrigger(syncId, {
        sync_mode: pendingMode,
        trigger,
      });
      onUpdated();
      setEditMode(false);
    } catch (err) {
      console.error('Failed to update trigger:', err);
    } finally {
      setSaving(false);
    }
  }, [syncId, pendingMode, scheduleConfig, onUpdated]);

  const handleCancel = useCallback(() => {
    setPendingMode(currentMode);
    setScheduleConfig(
      currentTrigger?.schedule ? { schedule: currentTrigger.schedule, timezone: currentTrigger.timezone || 'Asia/Shanghai' } : null,
    );
    setEditMode(false);
  }, [currentMode, currentTrigger]);

  const modeLabel = SYNC_MODE_META[currentMode]?.label || currentMode;

  return (
    <div style={{ padding: '0 4px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
        <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--po-text-subtle)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
          Sync Mode
        </div>
        {!isLocked && !editMode && (
          <button
            onClick={() => setEditMode(true)}
            style={{
              background: 'none', border: 'none', color: 'var(--po-text-disabled)', cursor: 'pointer',
              fontSize: 11, height: 30, padding: '0 6px', borderRadius: 4, transition: 'color 0.12s',
            }}
            onMouseEnter={e => { e.currentTarget.style.color = 'var(--po-text-muted)'; }}
            onMouseLeave={e => { e.currentTarget.style.color = 'var(--po-text-disabled)'; }}
          >
            Edit
          </button>
        )}
      </div>

      {!editMode ? (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px',
          background: 'var(--po-panel)', border: '1px solid var(--po-border-subtle)', borderRadius: 6,
        }}>
          <span style={{ fontSize: 12, fontWeight: 500, color: 'var(--po-text)' }}>{modeLabel}</span>
          {currentMode === 'scheduled' && currentTrigger?.schedule && (
            <span style={{ fontSize: 11, color: 'var(--po-text-disabled)' }}>· {describeCron(currentTrigger.schedule)}</span>
          )}
          {isLocked && (
            <span style={{ fontSize: 10, color: 'var(--po-text-disabled)', marginLeft: 'auto' }}>Fixed</span>
          )}
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {/* Mode buttons */}
          <div style={{ display: 'flex', gap: 4 }}>
            {policy.supportedModes.map(mode => (
              <button
                key={mode}
                onClick={() => setPendingMode(mode)}
                style={{
                  flex: 1, height: 32, borderRadius: 6, fontSize: 12, fontWeight: 500,
                  cursor: 'pointer', transition: 'all 0.12s',
                  background: pendingMode === mode ? 'color-mix(in srgb, var(--po-accent) 12%, transparent)' : 'transparent',
                  border: pendingMode === mode ? '1px solid color-mix(in srgb, var(--po-accent) 30%, transparent)' : '1px solid var(--po-border)',
                  color: pendingMode === mode ? 'var(--po-accent)' : 'var(--po-text-muted)',
                }}
              >
                {SYNC_MODE_META[mode]?.label || mode}
              </button>
            ))}
          </div>

          {/* Description */}
          <div style={{ fontSize: 11, color: 'var(--po-text-disabled)', padding: '0 2px' }}>
            {SYNC_MODE_META[pendingMode]?.desc}
          </div>

          {/* Schedule config when "scheduled" is selected */}
          {pendingMode === 'scheduled' && (
            <ScheduleEditor
              config={scheduleConfig}
              onChange={setScheduleConfig}
            />
          )}

          {/* Save / Cancel */}
          <div style={{ display: 'flex', gap: 6, paddingTop: 4 }}>
            <button
              onClick={handleSave}
              disabled={saving || (pendingMode === 'scheduled' && !scheduleConfig?.schedule)}
              style={{
                flex: 1, height: 30, borderRadius: 6, fontSize: 12, fontWeight: 500,
                background: 'color-mix(in srgb, var(--po-accent) 12%, transparent)', border: '1px solid color-mix(in srgb, var(--po-accent) 25%, transparent)',
                color: 'var(--po-accent)', cursor: saving ? 'not-allowed' : 'pointer',
                opacity: saving || (pendingMode === 'scheduled' && !scheduleConfig?.schedule) ? 0.5 : 1,
                display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 6,
              }}
            >
              {saving && <Dots size='xs' tone='info' />}
              {saving ? 'Saving…' : 'Save'}
            </button>
            <button
              onClick={handleCancel}
              style={{
                flex: 1, height: 30, borderRadius: 6, fontSize: 12, fontWeight: 500,
                background: 'transparent', border: '1px solid var(--po-border)',
                color: 'var(--po-text-muted)', cursor: 'pointer',
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ============================================================
// ScheduleEditor — lightweight inline cron builder
// ============================================================

function ScheduleEditor({
  config,
  onChange,
}: {
  config: { schedule?: string; timezone?: string } | null;
  onChange: (c: { schedule?: string; timezone?: string }) => void;
}) {
  const [hour, setHour] = useState(9);
  const [minute, setMinute] = useState(0);
  const [repeatType, setRepeatType] = useState<'daily' | 'weekly'>('daily');
  const [weekday, setWeekday] = useState(1);
  const initialized = useRef(false);

  useEffect(() => {
    if (initialized.current) return;
    initialized.current = true;
    if (!config?.schedule) {
      onChange({ schedule: `0 9 * * *`, timezone: 'Asia/Shanghai' });
      return;
    }
    const parts = config.schedule.split(' ');
    if (parts.length < 5) return;
    const m = parseInt(parts[0], 10);
    const h = parseInt(parts[1], 10);
    if (!isNaN(m)) setMinute(m);
    if (!isNaN(h)) setHour(h);
    if (parts[4] !== '*' && parts[2] === '*') {
      setRepeatType('weekly');
      const d = parseInt(parts[4], 10);
      if (!isNaN(d)) setWeekday(d);
    } else {
      setRepeatType('daily');
    }
  }, [config, onChange]);

  const buildCron = useCallback((h: number, m: number, rpt: typeof repeatType, wd: number) => {
    const cron = rpt === 'weekly' ? `${m} ${h} * * ${wd}` : `${m} ${h} * * *`;
    onChange({ schedule: cron, timezone: 'Asia/Shanghai' });
  }, [onChange]);

  const dayLabels = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

  const selectStyle: React.CSSProperties = {
    height: 30, padding: '0 6px', borderRadius: 4, fontSize: 12,
    background: 'var(--po-panel)', border: '1px solid var(--po-active)', color: 'var(--po-text)',
    cursor: 'pointer', outline: 'none',
  };

  return (
    <div style={{
      padding: '10px 12px', borderRadius: 6,
      background: 'var(--po-panel)', border: '1px solid var(--po-border-subtle)',
      display: 'flex', flexDirection: 'column', gap: 8,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <select
          value={repeatType}
          onChange={e => {
            const v = e.target.value as typeof repeatType;
            setRepeatType(v);
            buildCron(hour, minute, v, weekday);
          }}
          style={selectStyle}
        >
          <option value="daily">Daily</option>
          <option value="weekly">Weekly</option>
        </select>
        {repeatType === 'weekly' && (
          <select
            value={weekday}
            onChange={e => {
              const d = parseInt(e.target.value, 10);
              setWeekday(d);
              buildCron(hour, minute, repeatType, d);
            }}
            style={selectStyle}
          >
            {dayLabels.map((label, i) => (
              <option key={i} value={i}>{label}</option>
            ))}
          </select>
        )}
        <span style={{ fontSize: 12, color: 'var(--po-text-disabled)' }}>at</span>
        <select
          value={hour}
          onChange={e => {
            const h = parseInt(e.target.value, 10);
            setHour(h);
            buildCron(h, minute, repeatType, weekday);
          }}
          style={{ ...selectStyle, width: 52 }}
        >
          {Array.from({ length: 24 }, (_, i) => (
            <option key={i} value={i}>{i.toString().padStart(2, '0')}</option>
          ))}
        </select>
        <span style={{ fontSize: 12, color: 'var(--po-text-disabled)' }}>:</span>
        <select
          value={minute}
          onChange={e => {
            const m = parseInt(e.target.value, 10);
            setMinute(m);
            buildCron(hour, m, repeatType, weekday);
          }}
          style={{ ...selectStyle, width: 52 }}
        >
          {[0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55].map(m => (
            <option key={m} value={m}>{m.toString().padStart(2, '0')}</option>
          ))}
        </select>
      </div>
      <div style={{ fontSize: 11, color: 'var(--po-text-disabled)' }}>
        {repeatType === 'daily' ? `Every day at ${hour.toString().padStart(2, '0')}:${minute.toString().padStart(2, '0')}` : `Every ${dayLabels[weekday]} at ${hour.toString().padStart(2, '0')}:${minute.toString().padStart(2, '0')}`}
      </div>
    </div>
  );
}

// ============================================================
// Helpers
// ============================================================

function describeCron(schedule: string): string {
  const parts = schedule.split(' ');
  if (parts.length < 5) return schedule;
  const [min, hour, day, month, weekday] = parts;

  const time = `${hour.padStart(2, '0')}:${min.padStart(2, '0')}`;

  if (weekday !== '*' && day === '*') return `Weekly at ${time}`;
  if (day === '*' && month === '*' && weekday === '*') return `Daily at ${time}`;
  return `At ${time}`;
}

// ============================================================
// Sub-components
// ============================================================

function ActionButton({ label, icon, variant = 'default', onClick }: {
  label: string; icon: string; variant?: 'default' | 'danger'; onClick: () => void;
}) {
  const isDanger = variant === 'danger';
  const textColor = isDanger ? 'var(--po-text-muted)' : 'var(--po-text)';
  return (
    <button
      onClick={onClick}
      style={{
        flex: 1, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 5,
        height: 30, padding: '0 10px', borderRadius: 6,
        background: 'transparent',
        border: '1px solid var(--po-border)',
        color: textColor, fontSize: 12, fontWeight: 500, cursor: 'pointer',
        transition: 'all 0.12s',
        whiteSpace: 'nowrap',
      }}
      onMouseEnter={e => {
        e.currentTarget.style.background = isDanger ? 'color-mix(in srgb, var(--po-danger) 8%, transparent)' : 'var(--po-hover)';
        e.currentTarget.style.borderColor = isDanger ? 'color-mix(in srgb, var(--po-danger) 20%, transparent)' : 'var(--po-border-strong)';
        if (isDanger) e.currentTarget.style.color = 'var(--po-danger)';
      }}
      onMouseLeave={e => {
        e.currentTarget.style.background = 'transparent';
        e.currentTarget.style.borderColor = 'var(--po-border)';
        e.currentTarget.style.color = textColor;
      }}
    >
      {icon === 'pause' && (
        <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>
      )}
      {icon === 'play' && (
        <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M5 3L19 12L5 21V3Z"/></svg>
      )}
      {icon === 'retry' && (
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="23 4 23 10 17 10" /><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
        </svg>
      )}
      {icon === 'disconnect' && (
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      )}
      {label}
    </button>
  );
}

function DetailRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 8,
      padding: '6px 0',
      borderBottom: '1px solid var(--po-hover)',
    }}>
      <div style={{ fontSize: 11, color: 'var(--po-text-disabled)', fontWeight: 500, flexShrink: 0, width: 72 }}>{label}</div>
      <div style={{
        flex: 1, fontSize: mono ? 11 : 12, color: 'var(--po-text-muted)',
        fontFamily: mono ? "var(--po-font-sans)" : 'inherit',
        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
      }}>
        {value}
      </div>
    </div>
  );
}
