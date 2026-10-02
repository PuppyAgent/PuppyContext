'use client';

import { PulseGrid } from '@/components/loading';
import { CountBadge } from '@/components/ui/CountBadge';
import { usePendingItems } from '@/features/history/pendingConflicts';
import {
  listKinds,
  type NeedsActionItem,
  type NeedsActionKindDef,
  type ResolvedResult,
} from '@/lib/needsActionRegistry';
import { isSnoozed } from '@/lib/needsActionSnooze';
import { useCallback, useEffect, useMemo, useState } from 'react';
import useSWR from 'swr';

// Side-effect import: populates the kind registry.
import '@/features/history/components/items';

export interface NeedsActionSelection {
  kind: string;
  itemId: string;
}

export interface NeedsActionSectionProps {
  projectId: string;
  seedItemsByKind?: Readonly<Record<string, readonly NeedsActionItem[] | undefined>>;
  selected: NeedsActionSelection | null;
  onSelect: (selection: NeedsActionSelection, item: NeedsActionItem) => void;
  onItemRemoved: (selection: NeedsActionSelection, result: ResolvedResult) => void;
  onSummaryChange?: (summary: NeedsActionSummary) => void;
}

export type NeedsActionSummary = {
  count: number;
  loading: boolean;
  hasErrors: boolean;
};

const DEFAULT_REFRESH_MS = 30_000;
const ROW_ITEM_HEIGHT = 46;
const ROW_MARGIN_Y = 1;
const ROW_HEIGHT = ROW_ITEM_HEIGHT + ROW_MARGIN_Y * 2;
const GRAPH_WIDTH = 20;
const DOT_X = GRAPH_WIDTH / 2;
const DOT_Y = 16;
const DOT_SIZE = 6;

type KindSnapshot = {
  items: NeedsActionItem[];
  loading: boolean;
  error: boolean;
};

type FlatNeedsActionItem = {
  def: NeedsActionKindDef;
  item: NeedsActionItem;
};

export function NeedsActionSection({
  projectId,
  seedItemsByKind,
  selected,
  onSelect,
  onItemRemoved: _onItemRemoved,
  onSummaryChange,
}: NeedsActionSectionProps) {
  const kinds = useMemo(() => listKinds(), []);
  const [expanded, setExpanded] = useState(true);
  const [snapshots, setSnapshots] = useState<Record<string, KindSnapshot>>({});
  const [snoozeTick, setSnoozeTick] = useState(0);

  const handleSnapshot = useCallback((kind: string, next: KindSnapshot) => {
    setSnapshots((current) => {
      const previous = current[kind];
      if (sameSnapshot(previous, next)) return current;
      return { ...current, [kind]: next };
    });
  }, []);

  const knownSnapshots = kinds
    .map((kind) => snapshots[kind.kind])
    .filter((snapshot): snapshot is KindSnapshot => Boolean(snapshot));
  const allKnown = knownSnapshots.length === kinds.length;
  const totalCount = knownSnapshots.reduce((sum, snapshot) => sum + snapshot.items.length, 0);
  const loading = !allKnown || knownSnapshots.some((snapshot) => snapshot.loading);
  const hasErrors = knownSnapshots.some((snapshot) => snapshot.error);

  const flatItems = useMemo<FlatNeedsActionItem[]>(() => {
    const rows: FlatNeedsActionItem[] = [];
    for (const def of kinds) {
      const snapshot = snapshots[def.kind];
      if (!snapshot) continue;
      for (const item of snapshot.items) rows.push({ def, item });
    }
    return rows.sort(compareNeedsActionRows);
  }, [kinds, snapshots]);

  useEffect(() => {
    onSummaryChange?.({
      count: totalCount,
      loading,
      hasErrors,
    });
  }, [hasErrors, loading, onSummaryChange, totalCount]);

  return (
    <section
      style={{
        background: 'var(--po-canvas)',
        display: 'flex',
        flex: '0 0 auto',
        flexDirection: 'column',
        minHeight: 0,
        borderBottom: '1px solid var(--po-divider)',
      }}
    >
      <button
        type="button"
        onClick={() => setExpanded((open) => !open)}
        style={{
          width: '100%',
          height: 42,
          minHeight: 42,
          color: 'var(--po-text)',
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '0 8px 0 16px',
          border: 0,
          background: 'transparent',
          cursor: 'pointer',
          textAlign: 'left',
        }}
      >
        <svg
          aria-hidden
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.25"
          strokeLinecap="round"
          strokeLinejoin="round"
          style={{
            color: 'var(--po-text-subtle)',
            flexShrink: 0,
            transform: expanded ? 'rotate(90deg)' : 'rotate(0deg)',
            transition: 'transform 120ms ease',
          }}
        >
          <path d="m9 18 6-6-6-6" />
        </svg>
        <span
          style={{
            flex: 1,
            minWidth: 0,
            fontSize: 12,
            fontWeight: 600,
            color: 'var(--po-text)',
          }}
        >
          Needs action
        </span>
        {totalCount > 0 ? (
          <CountBadge
            value={totalCount}
            ariaLabel={`${totalCount} open items`}
            size="md"
            tone="danger"
          />
        ) : null}
      </button>

      <KindDataSubscriptions
        kinds={kinds}
        projectId={projectId}
        seedItemsByKind={seedItemsByKind}
        snoozeTick={snoozeTick}
        onSnapshot={handleSnapshot}
      />

      {expanded ? (
        <div
          className="custom-scrollbar"
          style={{
            flex: '0 0 auto',
            minHeight: 0,
            overflowY: 'visible',
            padding: totalCount > 0 ? '0 0 10px' : '0 8px 14px 36px',
            display: 'flex',
            flexDirection: 'column',
            gap: 0,
          }}
        >
          {kinds.length === 0 ? (
            <EmptyState />
          ) : (
            <>
              {flatItems.length > 0 ? (
                <FlatNeedsActionRows
                  rows={flatItems}
                  selected={selected}
                  projectId={projectId}
                  onSelect={onSelect}
                />
              ) : null}
              {loading && totalCount === 0 ? <LoadingState /> : null}
              {!loading && hasErrors && totalCount === 0 ? <EmptyState hasErrors /> : null}
              {!loading && !hasErrors && allKnown && totalCount === 0 ? <EmptyState /> : null}
            </>
          )}
        </div>
      ) : null}
    </section>
  );
}

function KindDataSubscriptions({
  kinds,
  projectId,
  seedItemsByKind,
  snoozeTick,
  onSnapshot,
}: {
  kinds: readonly NeedsActionKindDef[];
  projectId: string;
  seedItemsByKind?: Readonly<Record<string, readonly NeedsActionItem[] | undefined>>;
  snoozeTick: number;
  onSnapshot: (kind: string, snapshot: KindSnapshot) => void;
}) {
  return (
    <>
      {kinds.map((def) => (
        <KindDataContainer
          key={def.kind}
          def={def}
          projectId={projectId}
          seedItems={seedItemsByKind?.[def.kind]}
          snoozeTick={snoozeTick}
          onSnapshot={onSnapshot}
        />
      ))}
    </>
  );
}

function KindDataContainer({
  def,
  projectId,
  seedItems,
  snoozeTick,
  onSnapshot,
}: {
  def: NeedsActionKindDef;
  projectId: string;
  seedItems?: readonly NeedsActionItem[];
  snoozeTick: number;
  onSnapshot: (kind: string, snapshot: KindSnapshot) => void;
}) {
  const sharedKind = def.kind === 'conflict' || def.kind === 'pending-review';
  const pending = usePendingItems(projectId, def.kind, sharedKind && seedItems === undefined);
  const query = useSWR<NeedsActionItem[]>(
    seedItems === undefined && !sharedKind ? [`needs-action:${def.kind}`, projectId] : null,
    () => def.fetchItems(projectId) as Promise<NeedsActionItem[]>,
    {
      refreshInterval: def.refreshIntervalMs ?? DEFAULT_REFRESH_MS,
      refreshWhenHidden: false,
      keepPreviousData: false,
    },
  );

  const { data, error } = sharedKind ? pending : query;

  const visibleItems = useMemo(() => {
    const source = seedItems ?? data;
    if (!source) return [] as NeedsActionItem[];
    return source.filter(
      (item) => !isSnoozed({ projectId, kind: def.kind, id: item.id }),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, def.kind, projectId, seedItems, snoozeTick]);

  useEffect(() => {
    onSnapshot(def.kind, {
      items: visibleItems,
      loading: seedItems === undefined && !data && !error,
      error: Boolean(error),
    });
  }, [data, def.kind, error, onSnapshot, seedItems, visibleItems]);

  return null;
}

function FlatNeedsActionRows({
  rows,
  selected,
  projectId: _projectId,
  onSelect,
}: {
  rows: readonly FlatNeedsActionItem[];
  selected: NeedsActionSelection | null;
  projectId: string;
  onSelect: NeedsActionSectionProps['onSelect'];
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 0 }}>
      {rows.map(({ def, item }) => (
        <FlatNeedsActionRow
          key={`${def.kind}:${item.id}`}
          def={def}
          item={item}
          selected={selected?.kind === def.kind && selected.itemId === item.id}
          onSelect={() => onSelect({ kind: def.kind, itemId: item.id }, item)}
        />
      ))}
    </div>
  );
}

function FlatNeedsActionRow({
  def,
  item,
  selected,
  onSelect,
}: {
  def: NeedsActionKindDef;
  item: NeedsActionItem;
  selected: boolean;
  onSelect: () => void;
}) {
  const row = describeNeedsActionRow(def, item);
  return (
    <div style={{ position: 'relative', height: ROW_HEIGHT }}>
      <button
        type="button"
        onClick={onSelect}
        style={{
          display: 'flex',
          alignItems: 'stretch',
          width: 'calc(100% - 12px)',
          margin: `${ROW_MARGIN_Y}px 6px`,
          height: ROW_ITEM_HEIGHT,
          boxSizing: 'border-box',
          border: 0,
          borderRadius: 6,
          background: selected ? 'var(--po-selected)' : 'transparent',
          color: selected ? 'var(--po-text)' : 'var(--po-text-muted)',
          cursor: 'pointer',
          padding: 0,
          textAlign: 'left',
          transition: 'background 0.1s, color 0.1s',
        }}
        onMouseEnter={(event) => {
          if (!selected) event.currentTarget.style.background = 'var(--po-hover)';
        }}
        onMouseLeave={(event) => {
          if (!selected) event.currentTarget.style.background = 'transparent';
        }}
      >
        <div
          style={{
            flex: 1,
            minWidth: 0,
            display: 'flex',
            alignItems: 'flex-start',
            height: '100%',
            boxSizing: 'border-box',
            paddingLeft: 6,
            paddingRight: 6,
          }}
        >
          <svg
            width={GRAPH_WIDTH}
            height={ROW_HEIGHT}
            viewBox={`0 0 ${GRAPH_WIDTH} ${ROW_HEIGHT}`}
            style={{
              flexShrink: 0,
              marginTop: -ROW_MARGIN_Y,
              marginBottom: -ROW_MARGIN_Y,
              overflow: 'visible',
              pointerEvents: 'none',
            }}
          >
            <rect
              x={DOT_X - DOT_SIZE / 2}
              y={DOT_Y - DOT_SIZE / 2}
              width={DOT_SIZE}
              height={DOT_SIZE}
              rx={1.5}
              fill={row.dotColor}
            />
          </svg>

          <div
            style={{
              flex: 1,
              minWidth: 0,
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              height: '100%',
              paddingLeft: 4,
            }}
          >
            <div
              style={{
                flex: 1,
                minWidth: 0,
                display: 'flex',
                flexDirection: 'column',
                justifyContent: 'center',
                gap: 2,
              }}
            >
              <span
                style={{
                  minWidth: 0,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                  color: 'var(--po-text)',
                  fontSize: 12,
                  fontWeight: 600,
                  lineHeight: '14px',
                }}
                title={row.title}
              >
                {row.title}
              </span>
              <span
                style={{
                  minWidth: 0,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                  color: 'var(--po-text-subtle)',
                  fontSize: 11,
                  lineHeight: '12px',
                }}
                title={row.meta}
              >
                {row.meta}
              </span>
            </div>

            <span
              style={{
                height: 18,
                minWidth: 48,
                padding: '0 7px',
                borderRadius: 999,
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: row.tagColor,
                background: row.tagBackground,
                border: `1px solid ${row.tagBorder}`,
                fontSize: 10,
                fontWeight: 500,
                letterSpacing: '0.02em',
                lineHeight: '18px',
                flexShrink: 0,
                opacity: 0.82,
              }}
            >
              {row.tag}
            </span>
          </div>
        </div>
      </button>
    </div>
  );
}

function LoadingState() {
  return (
    <div
      style={{
        minHeight: 44,
        padding: '0 4px 2px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'flex-start',
      }}
    >
      <PulseGrid size="sm" ariaLabel="Loading pending reviews" />
    </div>
  );
}

function EmptyState({
  hasErrors = false,
}: {
  readonly hasErrors?: boolean;
}) {
  return (
    <div
      style={{
        minHeight: 44,
        padding: '0 4px 2px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'flex-start',
        fontSize: 12,
        color: 'var(--po-text-subtle)',
        lineHeight: '18px',
        textAlign: 'left',
      }}
    >
      {hasErrors ? 'Some pending checks are unavailable.' : 'No pending reviews.'}
    </div>
  );
}

function describeNeedsActionRow(def: NeedsActionKindDef, item: NeedsActionItem) {
  const tag = statusTagForItem(item);
  const age = item.created_at ? formatRelative(item.created_at) : '';

  if (item.kind === 'failed-sync') {
    const apName = item.source.synchronize_binding_name || item.source.provider || def.label;
    return {
      title: apName,
      meta: joinMeta([
        item.source.direction || 'sync',
        item.source.target_path || item.scope_path || item.source.provider,
        age,
      ]),
      dotColor: tag.color,
      ...tag,
    };
  }

  if (item.kind === 'pending-review') {
    const fileCount = item.source.changed_paths?.length ?? 0;
    return {
      title: formatScope(item.scope_path),
      meta: joinMeta([
        `${fileCount} file${fileCount === 1 ? '' : 's'}`,
        item.source.resolver_actor || 'Agent',
        age,
      ]),
      dotColor: def.accentVar,
      ...tag,
    };
  }

  if (item.kind === 'conflict') {
    const fileCount = item.source.changed_paths?.length ?? 0;
    return {
      title: formatScope(item.scope_path),
      meta: joinMeta([
        `${fileCount} file${fileCount === 1 ? '' : 's'}`,
        '3-way merge',
        age,
      ]),
      dotColor: def.accentVar,
      ...tag,
    };
  }

  if (item.kind === 'risky-delete') {
    return {
      title: `${item.source.deleted_count} files deleted`,
      meta: joinMeta([
        item.source.who || 'unknown',
        age,
        item.source.commit_id.slice(0, 8),
      ]),
      dotColor: def.accentVar,
      ...tag,
    };
  }

  return {
    title: def.label,
    meta: joinMeta([def.label, age]),
    dotColor: def.accentVar,
    ...tag,
  };
}

function statusTagForItem(item: NeedsActionItem) {
  if (item.kind === 'failed-sync') {
    return {
      tag: 'Failed',
      color: 'var(--po-danger)',
      tagColor: 'color-mix(in srgb, var(--po-danger) 72%, var(--po-text-muted))',
      tagBackground: 'color-mix(in srgb, var(--po-danger) 6%, transparent)',
      tagBorder: 'color-mix(in srgb, var(--po-danger) 18%, transparent)',
    };
  }
  if (item.kind === 'conflict') {
    return {
      tag: 'Pending',
      color: 'var(--po-warning)',
      tagColor: 'var(--po-warning)',
      tagBackground: 'color-mix(in srgb, var(--po-warning) 11%, transparent)',
      tagBorder: 'color-mix(in srgb, var(--po-warning) 28%, transparent)',
    };
  }
  return {
    tag: 'Review',
    color: 'var(--po-accent)',
    tagColor: 'var(--po-accent)',
    tagBackground: 'color-mix(in srgb, var(--po-accent) 10%, transparent)',
    tagBorder: 'color-mix(in srgb, var(--po-accent) 25%, transparent)',
  };
}

function compareNeedsActionRows(a: FlatNeedsActionItem, b: FlatNeedsActionItem): number {
  const priorityDelta = needsActionPriority(a.item) - needsActionPriority(b.item);
  if (priorityDelta !== 0) return priorityDelta;
  const timeA = a.item.created_at ? new Date(a.item.created_at).getTime() : 0;
  const timeB = b.item.created_at ? new Date(b.item.created_at).getTime() : 0;
  return timeB - timeA;
}

function needsActionPriority(item: NeedsActionItem): number {
  if (item.kind === 'failed-sync') return 0;
  if (item.kind === 'conflict') return 1;
  return 2;
}

function sameSnapshot(previous: KindSnapshot | undefined, next: KindSnapshot): boolean {
  if (!previous) return false;
  if (previous.loading !== next.loading || previous.error !== next.error) return false;
  if (previous.items.length !== next.items.length) return false;
  return previous.items.every((item, index) => {
    const other = next.items[index];
    return other && item.id === other.id && item.created_at === other.created_at;
  });
}

function joinMeta(parts: Array<string | null | undefined>): string {
  return parts.filter((part): part is string => Boolean(part)).join(' · ');
}

function formatScope(scope: string): string {
  const normalized = (scope || '').replace(/^\/+|\/+$/g, '');
  return normalized || 'Root';
}

function formatRelative(iso: string): string {
  const ts = new Date(iso).getTime();
  if (!Number.isFinite(ts)) return '';
  const diff = Date.now() - ts;
  if (diff < 60_000) return 'just now';
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return `${Math.floor(diff / 86_400_000)}d ago`;
}

export { getKind,listKinds,type NeedsActionItem } from '@/lib/needsActionRegistry';
