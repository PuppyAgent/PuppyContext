'use client';

import { Dots } from '@/components/loading';
import { StatusDot } from '@/components/ui/StatusDot';
import { PROJECT_CONTENT_RAIL_WIDTH } from '@/lib/layout';
import {
  registerKind,
  type FailedSyncItem,
  type NeedsActionRenderContext,
} from '@/lib/needsActionRegistry';
import { snoozeUntil } from '@/lib/needsActionSnooze';
import { listFailedSynchronizeRuns, resumeSynchronizeBinding } from '@/lib/synchronizeApi';
import React, { useState } from 'react';

/**
 * Failed sync kind: a sync_run row with ``status='failed'``. Fills
 * PUP-5 gap G1 — sync jobs always existed in the connectors layer
 * but had no list endpoint exposed to the frontend.
 *
 * The row shows the binding name + provider; the detail pane
 * shows the captured ``error`` so the user can see what broke. The
 * primary action is "Retry" (POST resume on the binding); the
 * secondary is "Snooze 24h" (client-only — sync runs naturally clear
 * from the list as new successful runs arrive).
 */

const KIND_LABEL = 'Failed sync';
const ACCENT_VAR = 'var(--po-danger)';

async function fetchItems(projectId: string): Promise<FailedSyncItem[]> {
  const rows = await listFailedSynchronizeRuns(projectId);
  return rows.map<FailedSyncItem>((r) => ({
    kind: 'failed-sync',
    id: r.id,
    // Scope path on the row is the binding destination. Falls
    // back to the provider label so the row never renders blank when
    // a sync was configured without an explicit path.
    scope_path: r.target_path || `(${r.provider})`,
    created_at: r.started_at || r.finished_at || undefined,
    source: r,
  }));
}

function renderRow(item: FailedSyncItem, ctx: NeedsActionRenderContext): React.ReactNode {
  return <FailedSyncRow item={item} ctx={ctx} />;
}

function renderDetail(item: FailedSyncItem, ctx: NeedsActionRenderContext): React.ReactNode {
  return <FailedSyncDetail item={item} ctx={ctx} />;
}

registerKind<FailedSyncItem>({
  kind: 'failed-sync',
  label: KIND_LABEL,
  description: 'Sync runs that failed and need a retry or fix',
  accentVar: ACCENT_VAR,
  fetchItems,
  refreshIntervalMs: 60_000,
  renderRow,
  renderDetail,
});

// ── Row ──────────────────────────────────────────────────────────────

function FailedSyncRow({
  item,
  ctx,
}: {
  item: FailedSyncItem;
  ctx: NeedsActionRenderContext;
}) {
  const apName = item.source.synchronize_binding_name || item.source.provider;
  return (
    <button
      type="button"
      onClick={ctx.onSelect}
      className={`group flex w-full min-w-0 items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors ${
        ctx.isSelected ? 'bg-[var(--po-selected)]' : 'hover:bg-[var(--po-hover)]'
      }`}
    >
      <StatusDot style={{ background: ACCENT_VAR }} />
      <div className="min-w-0 flex-1">
        <div className="truncate text-[12px] font-medium text-[var(--po-text)]">
          {apName}
        </div>
        <div className="truncate text-[11px] text-[var(--po-text-subtle)]">
          {item.source.direction || 'sync'}
          {item.source.target_path ? ` · ${item.source.target_path}` : ''}
          {item.created_at ? ` · ${formatRelative(item.created_at)}` : ''}
        </div>
      </div>
      <span className="text-[10px] font-medium uppercase tracking-wide text-[var(--po-text-subtle)] opacity-0 transition-opacity group-hover:opacity-100">
        Retry
      </span>
    </button>
  );
}

// ── Detail ───────────────────────────────────────────────────────────

function FailedSyncDetail({
  item,
  ctx,
}: {
  item: FailedSyncItem;
  ctx: NeedsActionRenderContext;
}) {
  const [busy, setBusy] = useState<'retry' | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleRetry = async () => {
    setBusy('retry');
    setError(null);
    try {
      await resumeSynchronizeBinding(item.source.synchronize_binding_id);
      // Resume returns immediately; the run kicks off async. Treat
      // this as "dismissed" — the row will reappear if the new run
      // also fails, and disappear when a successful run lands.
      ctx.onResolved({ reason: 'dismissed' });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Retry failed');
      setBusy(null);
    }
  };

  const handleSnooze = () => {
    snoozeUntil({ projectId: ctx.projectId, kind: 'failed-sync', id: item.id });
    ctx.onSnoozed();
  };

  const apName = item.source.synchronize_binding_name || item.source.provider;
  const started = item.source.started_at ? formatDateTime(item.source.started_at) : '';
  const finished = item.source.finished_at ? formatDateTime(item.source.finished_at) : '';

  return (
    <div
      className="p-6 md:p-8 mx-auto"
      style={{
        width: '100%',
        maxWidth: PROJECT_CONTENT_RAIL_WIDTH,
        boxSizing: 'border-box',
      }}
    >
      <div className="flex flex-wrap items-center gap-4 mb-6">
        <div className="flex min-w-0 items-center gap-3">
          <span
            className="text-lg font-medium text-[var(--po-text)] font-sans"
            title={item.id}
          >
            {item.id.slice(0, 8)}
          </span>
          <span className="truncate text-sm text-[var(--po-text-muted)]">
            Sync failed for {apName}
          </span>
        </div>

        <div className="ml-auto flex flex-wrap items-center gap-3">
          <span
            className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium"
            style={{
              color: ACCENT_VAR,
              background: `color-mix(in srgb, ${ACCENT_VAR} 10%, transparent)`,
              borderColor: `color-mix(in srgb, ${ACCENT_VAR} 20%, transparent)`,
            }}
          >
            <StatusDot style={{ background: ACCENT_VAR }} />
            Failed sync
          </span>

          {item.created_at && (
            <span className="text-xs font-medium text-[var(--po-text-subtle)]">
              {formatRelative(item.created_at)}
            </span>
          )}

          <span className="rounded border border-[var(--po-border-subtle)] bg-[var(--po-control)] px-2 py-1 font-sans text-xs text-[var(--po-text-subtle)]">
            {item.source.provider}
          </span>
        </div>
      </div>

      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          fontSize: 11,
          fontFamily: 'var(--po-font-sans)',
          color: 'var(--po-text-subtle)',
          marginBottom: 16,
          paddingBottom: 14,
          borderBottom: '1px solid var(--po-border-subtle)',
        }}
      >
        <span>1 failed run</span>
        <span>Provider {item.source.provider}</span>
        <span>Direction {item.source.direction}</span>
        {item.source.duration_ms != null ? <span>{item.source.duration_ms} ms</span> : null}
      </div>

      <section
        className="rounded-xl border p-4"
        style={{
          borderColor: 'var(--po-border-subtle)',
          background: 'var(--po-canvas)',
          marginBottom: 18,
        }}
      >
        <div className="mb-3 text-xs font-medium text-[var(--po-text-muted)]">
          Run details
        </div>
        <ul style={kvListStyle}>
          <KV label="Provider" value={item.source.provider} />
          <KV label="Direction" value={item.source.direction} />
          {item.source.target_path && (
            <KV label="Path" value={item.source.target_path} mono />
          )}
          {started && <KV label="Started" value={started} />}
          {finished && <KV label="Finished" value={finished} />}
          {item.source.duration_ms != null && (
            <KV label="Duration" value={`${item.source.duration_ms} ms`} />
          )}
          {item.source.trigger_type && (
            <KV label="Trigger" value={item.source.trigger_type} />
          )}
        </ul>
      </section>

      {item.source.error ? (
        <section
          className="rounded-xl border p-4"
          style={{
            borderColor: 'color-mix(in srgb, var(--po-danger) 18%, var(--po-border-subtle))',
            background: 'color-mix(in srgb, var(--po-danger) 3%, var(--po-canvas))',
            marginBottom: 18,
          }}
        >
          <div
            className="mb-3 text-xs font-medium"
            style={{ color: 'color-mix(in srgb, var(--po-danger) 78%, var(--po-text-muted))' }}
          >
            Error
          </div>
          <pre
            style={{
              margin: 0,
              fontSize: 12,
              lineHeight: 1.55,
              color: 'var(--po-text)',
              fontFamily: 'var(--po-font-mono, ui-monospace, monospace)',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              overflow: 'auto',
            }}
          >
            {item.source.error}
          </pre>
        </section>
      ) : null}

      {item.source.result_summary ? (
        <section style={{ marginBottom: 18 }}>
          <div className="mb-2 text-xs font-medium text-[var(--po-text-muted)]">
            Result summary
          </div>
          <div style={{ fontSize: 12, color: 'var(--po-text-muted)', whiteSpace: 'pre-wrap' }}>
            {item.source.result_summary}
          </div>
        </section>
      ) : null}

      {error && (
        <div className="mb-4 text-[13px] text-[var(--po-danger)]" role="alert">
          {error}
        </div>
      )}

      <div className="flex items-center gap-3 pt-1">
        <button
          type="button"
          onClick={handleRetry}
          disabled={busy !== null}
          style={primaryButtonStyle(busy === 'retry')}
        >
          {busy === 'retry' ? <Dots size="xs" /> : null}
          {busy === 'retry' ? 'Retrying…' : 'Retry sync'}
        </button>
        <button
          type="button"
          onClick={handleSnooze}
          disabled={busy !== null}
          style={ghostButtonStyle(busy !== null)}
        >
          Snooze 24h
        </button>
      </div>
    </div>
  );
}

function KV({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <li style={{ display: 'flex', gap: 12, padding: '4px 0', fontSize: 12 }}>
      <span style={{ color: 'var(--po-text-subtle)', minWidth: 90 }}>{label}</span>
      <span
        style={{
          color: 'var(--po-text)',
          fontFamily: mono ? 'var(--po-font-mono, ui-monospace, monospace)' : undefined,
          wordBreak: 'break-all',
        }}
      >
        {value}
      </span>
    </li>
  );
}

function formatRelative(iso: string): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  const diff = Date.now() - t;
  if (diff < 60_000) return 'just now';
  if (diff < 3600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3600_000)}h ago`;
  return `${Math.floor(diff / 86_400_000)}d ago`;
}

function formatDateTime(iso: string): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  return new Date(t).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

const kvListStyle: React.CSSProperties = {
  margin: 0,
  padding: 0,
  listStyle: 'none',
};

function primaryButtonStyle(disabled: boolean): React.CSSProperties {
  return {
    height: 32,
    padding: '0 14px',
    borderRadius: 6,
    border: 'none',
    background: 'var(--po-text)',
    color: 'var(--po-text-inverse)',
    fontSize: 13,
    fontWeight: 600,
    cursor: disabled ? 'not-allowed' : 'pointer',
    opacity: disabled ? 0.5 : 1,
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
  };
}

function ghostButtonStyle(disabled: boolean): React.CSSProperties {
  return {
    height: 32,
    padding: '0 12px',
    borderRadius: 6,
    border: 'none',
    background: 'transparent',
    color: 'var(--po-text-subtle)',
    fontSize: 12,
    fontWeight: 500,
    cursor: disabled ? 'not-allowed' : 'pointer',
    opacity: disabled ? 0.5 : 1,
    marginLeft: 'auto',
  };
}
