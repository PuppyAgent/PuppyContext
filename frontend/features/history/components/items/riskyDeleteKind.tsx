'use client';

import { StatusDot } from '@/components/ui/StatusDot';
import { getProjectHistory } from '@/lib/contentTreeApi';
import {
  registerKind,
  type NeedsActionRenderContext,
  type RiskyDeleteItem,
} from '@/lib/needsActionRegistry';
import { snoozeUntil } from '@/lib/needsActionSnooze';
import React from 'react';

/**
 * Risky delete kind (PUP-5 §4 "risky delete / mass edit", Gap G2).
 *
 * Unlike pending-review / conflict (which block on a resolution), this
 * is a pure after-the-fact heads-up: "a recent commit deleted N files."
 * It reads version history — surfacing commits whose ``audit_detail`` /
 * ``changes`` show a mass deletion at or above the threshold — shows the
 * affected paths, and lets the user acknowledge (snooze 24h).
 *
 * It deliberately does NOT offer an inline "undo". Undoing a delete means
 * rolling the scope back to the commit's PARENT (the delete commit itself
 * still has the files gone), and the rollback endpoint is currently
 * root-scope only — getting that right per-scope belongs in the History
 * rollback UI, which already resolves scope. Surfacing a one-click button
 * here that rolls back to the wrong target (or 404s on a sub-scope) would
 * be worse than no button. Users undo from History → the commit → rollback.
 *
 * Data source is the project history API (``audit_detail`` is now
 * plumbed onto each commit), not ``version_conflicts``. Snooze is keyed by
 * commit_id so an acknowledged delete stays hidden.
 */

const KIND_LABEL = 'Risky delete';
const ACCENT_VAR = 'var(--po-warning)';

// Flag a commit when it removes at least this many files. PUP-5 used
// "50 files" as the illustrative example; 10 is a deliberately
// conservative floor that catches genuine mass deletes (rm -rf of a
// folder, bulk cleanup) without flagging routine 1-2 file edits.
const RISKY_DELETE_THRESHOLD = 10;
// Only look back over recent history — older deletes aren't actionable
// heads-ups, and the snooze handles anything the user already saw.
const HISTORY_LOOKBACK = 50;
const MAX_SAMPLE_PATHS = 50;

function deletedPathsFromCommit(commit: {
  changes?: Array<{ path: string; op?: string }>;
  audit_detail?: Record<string, unknown> | null;
}): string[] {
  // Prefer the per-change ops (authoritative list of what was deleted).
  const fromChanges = (commit.changes || [])
    .filter((c) => c.op === 'deleted')
    .map((c) => c.path);
  if (fromChanges.length > 0) return fromChanges;
  // Fall back to audit_detail.paths (bulk delete / rmdir stamp it).
  const ad = commit.audit_detail;
  if (ad && Array.isArray((ad as { paths?: unknown }).paths)) {
    return ((ad as { paths: unknown[] }).paths).map(String);
  }
  return [];
}

export function buildRiskyDeleteItems(commits: ReadonlyArray<{
  commit_id: string;
  who: string;
  message: string;
  changes?: Array<{ path: string; op?: string }>;
  conflicts?: unknown[];
  root_hash: string;
  scope_path: string;
  created_at: string | null;
  audit_detail?: Record<string, unknown> | null;
}>): RiskyDeleteItem[] {
  const items: RiskyDeleteItem[] = [];
  for (const commit of commits) {
    const deletedPaths = deletedPathsFromCommit(commit);
    if (deletedPaths.length < RISKY_DELETE_THRESHOLD) continue;
    items.push({
      kind: 'risky-delete',
      id: commit.commit_id,
      scope_path: commit.scope_path || '',
      created_at: commit.created_at || undefined,
      source: {
        commit_id: commit.commit_id,
        who: commit.who || '',
        message: commit.message || '',
        deleted_count: deletedPaths.length,
        deleted_paths: deletedPaths.slice(0, MAX_SAMPLE_PATHS),
        root_hash: commit.root_hash || '',
      },
    });
  }
  return items;
}

async function fetchItems(projectId: string): Promise<RiskyDeleteItem[]> {
  const history = await getProjectHistory(projectId, HISTORY_LOOKBACK);
  return buildRiskyDeleteItems(history.commits || []);
}

function renderRow(item: RiskyDeleteItem, ctx: NeedsActionRenderContext): React.ReactNode {
  return <RiskyDeleteRow item={item} ctx={ctx} />;
}

function renderDetail(item: RiskyDeleteItem, ctx: NeedsActionRenderContext): React.ReactNode {
  return <RiskyDeleteDetail item={item} ctx={ctx} />;
}

registerKind<RiskyDeleteItem>({
  kind: 'risky-delete',
  label: KIND_LABEL,
  description: 'Recent commits that deleted many files — review',
  accentVar: ACCENT_VAR,
  fetchItems,
  refreshIntervalMs: 60_000,
  renderRow,
  renderDetail,
});

// ── Row ──────────────────────────────────────────────────────────────

function RiskyDeleteRow({
  item,
  ctx,
}: {
  item: RiskyDeleteItem;
  ctx: NeedsActionRenderContext;
}) {
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
          {item.source.deleted_count} files deleted
        </div>
        <div className="truncate text-[11px] text-[var(--po-text-subtle)]">
          {item.source.who || 'unknown'}
          {item.created_at ? ` · ${formatRelative(item.created_at)}` : ''}
          {` · ${item.source.commit_id.slice(0, 8)}`}
        </div>
      </div>
      <span className="text-[10px] font-medium uppercase tracking-wide text-[var(--po-text-subtle)] opacity-0 transition-opacity group-hover:opacity-100">
        Review
      </span>
    </button>
  );
}

// ── Detail ───────────────────────────────────────────────────────────

function RiskyDeleteDetail({
  item,
  ctx,
}: {
  item: RiskyDeleteItem;
  ctx: NeedsActionRenderContext;
}) {
  const handleSnooze = () => {
    snoozeUntil({ projectId: ctx.projectId, kind: 'risky-delete', id: item.id });
    ctx.onSnoozed();
  };

  return (
    <div className="flex h-full flex-col">
      <header
        style={{
          padding: '14px 20px',
          borderBottom: '1px solid var(--po-divider)',
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          flexShrink: 0,
        }}
      >
        <span
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            padding: '2px 8px',
            borderRadius: 4,
            fontSize: 11,
            fontWeight: 600,
            color: ACCENT_VAR,
            background: `color-mix(in srgb, ${ACCENT_VAR} 10%, transparent)`,
            border: `1px solid color-mix(in srgb, ${ACCENT_VAR} 25%, transparent)`,
          }}
        >
          <StatusDot style={{ background: ACCENT_VAR }} />
          {KIND_LABEL}
        </span>
        <span style={{ fontSize: 13, color: 'var(--po-text)' }}>
          {item.source.deleted_count} files deleted
        </span>
        <span style={{ fontSize: 12, color: 'var(--po-text-subtle)' }}>
          #{item.source.commit_id.slice(0, 8)}
        </span>
        {item.created_at && (
          <span style={{ fontSize: 12, color: 'var(--po-text-subtle)' }}>
            · {formatRelative(item.created_at)}
          </span>
        )}
      </header>

      <div className="flex-1 overflow-y-auto custom-scrollbar" style={{ padding: 20 }}>
        <section style={{ marginBottom: 18 }}>
          <h3 style={sectionTitleStyle}>Commit</h3>
          <div style={{ fontSize: 12, color: 'var(--po-text-muted)', lineHeight: 1.6 }}>
            <div><span style={{ color: 'var(--po-text-subtle)' }}>by</span> {item.source.who || 'unknown'}</div>
            {item.source.message && (
              <div><span style={{ color: 'var(--po-text-subtle)' }}>message</span> {item.source.message}</div>
            )}
          </div>
        </section>

        <section>
          <h3 style={sectionTitleStyle}>
            Deleted files{' '}
            <span style={{ color: 'var(--po-text-subtle)', fontWeight: 400 }}>
              ({item.source.deleted_count})
            </span>
          </h3>
          <ul style={{ margin: 0, padding: 0, listStyle: 'none' }}>
            {item.source.deleted_paths.map((p) => (
              <li
                key={p}
                style={{
                  fontSize: 12,
                  color: 'var(--po-text-muted)',
                  padding: '3px 8px',
                  fontFamily: 'var(--po-font-mono, ui-monospace, monospace)',
                }}
              >
                {p}
              </li>
            ))}
            {item.source.deleted_count > item.source.deleted_paths.length && (
              <li style={{ padding: '3px 8px', fontSize: 12, color: 'var(--po-text-subtle)' }}>
                …and {item.source.deleted_count - item.source.deleted_paths.length} more
              </li>
            )}
          </ul>
        </section>

        <p style={{ marginTop: 16, fontSize: 12, color: 'var(--po-text-subtle)', lineHeight: 1.5 }}>
          To undo this, open the commit in History and use its rollback —
          that path resolves the correct scope and target.
        </p>
      </div>

      <footer
        style={{
          padding: '12px 20px',
          borderTop: '1px solid var(--po-divider)',
          display: 'flex',
          gap: 8,
          flexShrink: 0,
          background: 'var(--po-canvas)',
        }}
      >
        <button
          type="button"
          onClick={handleSnooze}
          style={ghostButtonStyle(false)}
        >
          Snooze 24h
        </button>
      </footer>
    </div>
  );
}

// ── Helpers / styles ─────────────────────────────────────────────────

function formatRelative(iso: string): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  const diff = Date.now() - t;
  if (diff < 60_000) return 'just now';
  if (diff < 3600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3600_000)}h ago`;
  return `${Math.floor(diff / 86_400_000)}d ago`;
}

const sectionTitleStyle: React.CSSProperties = {
  fontSize: 12,
  fontWeight: 600,
  textTransform: 'uppercase',
  letterSpacing: '0.04em',
  color: 'var(--po-text-subtle)',
  margin: '0 0 8px 0',
};

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
