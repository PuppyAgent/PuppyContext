'use client';

/**
 * Paginated, filterable log of import/export runs. Pagination is
 * server-side via ``limit`` / ``offset``. Refresh-on-commit is
 * driven by the parent — when the parent receives a ``commit_update``
 * (or finishes a manual import/export) it bumps a ``refreshKey`` prop.
 */

import { T } from '@/features/files/components/github-integration/tokens';
import {
  listSynchronizeGithubLogs,
  type SynchronizeGithubLog,
  type SynchronizeGithubDirection,
  type SynchronizeGithubStatus,
} from '@/lib/synchronizeGithubApi';
import { useFormatter, useTranslations } from 'next-intl';
import { useEffect, useMemo, useState } from 'react';

interface Props {
  projectId: string;
  refreshKey: number;
}

const PAGE_SIZE = 20;

export function SyncLogTable({ projectId, refreshKey }: Readonly<Props>) {
  const t = useTranslations('integrations.github');
  const fmt = useFormatter();

  const [filter, setFilter] = useState<'all' | SynchronizeGithubDirection>('all');
  const [page, setPage] = useState(0);
  const [entries, setEntries] = useState<SynchronizeGithubLog[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    listSynchronizeGithubLogs(projectId, {
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
    })
      .then((res) => {
        if (cancelled) return;
        setEntries(res.entries);
        setTotal(res.total);
      })
      .catch((failure: unknown) => {
        if (cancelled) return;
        setError(failure instanceof Error ? failure.message : String(failure));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, page, refreshKey, retry]);

  const visibleEntries = useMemo(() => {
    if (filter === 'all') return entries;
    return entries.filter((e) => e.direction === filter);
  }, [entries, filter]);

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span style={{ color: T.text1, fontSize: 14, fontWeight: 500 }}>
          {t('syncLog')}
        </span>
        <div style={{ display: 'flex', gap: 4 }}>
          <FilterChip active={filter === 'all'} onClick={() => setFilter('all')}>
            {t('filterAll')}
          </FilterChip>
          <FilterChip active={filter === 'inbound'} onClick={() => setFilter('inbound')}>
            {t('filterImport')}
          </FilterChip>
          <FilterChip active={filter === 'outbound'} onClick={() => setFilter('outbound')}>
            {t('filterExport')}
          </FilterChip>
        </div>
      </div>

      <div
        style={{
          background: T.cardBg,
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 8,
          overflow: 'hidden',
        }}
      >
        {error && <div role="alert" style={{ padding: 16, color: T.danger }}>
          {error} <button type="button" aria-label="Retry GitHub history" onClick={() => setRetry(value => value + 1)}>↻</button>
        </div>}
        {loading && entries.length === 0 && (
          <div style={{ padding: 16, color: T.text3, fontSize: 12 }}>…</div>
        )}
        {!loading && !error && visibleEntries.length === 0 && (
          <div style={{ padding: 16, color: T.text3, fontSize: 12 }}>
            {t('syncLogEmpty')}
          </div>
        )}
        {visibleEntries.map((e) => (
          <SyncLogRow key={e.id} entry={e} fmt={fmt} t={t} />
        ))}
      </div>

      {pageCount > 1 && (
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, alignItems: 'center' }}>
          <button
            type="button"
            disabled={page === 0}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            style={pagerBtnStyle(page > 0)}
          >
            ‹
          </button>
          <span style={{ color: T.text3, fontSize: 11 }}>
            {page + 1} / {pageCount}
          </span>
          <button
            type="button"
            disabled={page + 1 >= pageCount}
            onClick={() => setPage((p) => p + 1)}
            style={pagerBtnStyle(page + 1 < pageCount)}
          >
            ›
          </button>
        </div>
      )}
    </div>
  );
}

function FilterChip({
  active,
  onClick,
  children,
}: Readonly<{
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}>) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        background: active ? 'var(--po-border)' : 'transparent',
        border: `1px solid ${active ? T.cardBorderStrong : T.cardBorder}`,
        borderRadius: 6,
        color: active ? T.text1 : T.text2,
        cursor: 'pointer',
        fontFamily: T.fontSans,
        fontSize: 12,
        height: 30,
        padding: '0 10px',
      }}
    >
      {children}
    </button>
  );
}

const STATUS_COLORS: Record<SynchronizeGithubStatus, string> = {
  pending: 'var(--po-text-muted)',
  success: 'var(--po-success)',
  failed: 'var(--po-danger)',
  conflict: 'var(--po-warning)',
};

function SyncLogRow({
  entry,
  fmt,
  t,
}: Readonly<{
  entry: SynchronizeGithubLog;
  fmt: ReturnType<typeof useFormatter>;
  t: ReturnType<typeof useTranslations<'integrations.github'>>;
}>) {
  const dirLabel = entry.direction === 'inbound' ? t('directionImport') : t('directionExport');
  const statusLabel = ({
    pending: t('statusPending'),
    success: t('statusSuccess'),
    failed: t('statusFailed'),
    conflict: t('statusConflict'),
  } as const)[entry.status];

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(72px, max-content) minmax(72px, max-content) 1fr max-content',
        gap: 12,
        alignItems: 'center',
        padding: '10px 14px',
        borderBottom: `1px solid ${T.cardBorder}`,
        fontSize: 12,
      }}
    >
      <span style={{ color: T.text2, fontFamily: T.fontSans }}>{dirLabel}</span>
      <span
        style={{
          color: STATUS_COLORS[entry.status],
          fontFamily: T.fontMono,
        }}
      >
        ● {statusLabel}
      </span>
      <span style={{ color: T.text3, fontFamily: T.fontMono, fontSize: 11 }}>
        {entry.git_sha && <span style={{ marginRight: 8 }}>git {entry.git_sha.slice(0, 12)}</span>}
        {entry.version_commit_id && <span>version {entry.version_commit_id.slice(0, 12)}</span>}
        {entry.error_message && (
          <span style={{ color: T.danger, marginLeft: 8 }} title={entry.error_message}>
            ⚠ {truncate(entry.error_message, 60)}
          </span>
        )}
        {entry.files_changed !== null && (
          <span style={{ marginLeft: 8 }}>· {t('filesChanged', { count: entry.files_changed })}</span>
        )}
      </span>
      <span style={{ color: T.text3, fontFamily: T.fontMono, fontSize: 11 }}>
        {fmt.dateTime(new Date(entry.created_at), 'short')}
      </span>
    </div>
  );
}

function pagerBtnStyle(enabled: boolean): React.CSSProperties {
  return {
    background: 'transparent',
    border: `1px solid ${T.cardBorder}`,
    borderRadius: 6,
    color: enabled ? T.text2 : T.text4,
    cursor: enabled ? 'pointer' : 'not-allowed',
    fontFamily: T.fontMono,
    fontSize: 12,
    height: 30,
    padding: '0 10px',
  };
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : `${s.slice(0, n - 1)}…`;
}
