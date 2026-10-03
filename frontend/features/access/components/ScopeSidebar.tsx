'use client';

import { useMemo, useState } from 'react';
import { repositoryViewKey, type AccessSurface, type RepositoryView } from '@/lib/repoApi';
import { CountBadge } from '@/components/ui/CountBadge';
import { StatusDot } from '@/components/ui/StatusDot';
import { SIDEBAR_ROW_TYPOGRAPHY } from '@/lib/uiTypography';
import { T } from '@/features/access/lib/tokens';

type ScopeFilter = 'all' | 'active' | 'inactive';

export function ScopeSidebar({
  scopes,
  connectorsByTarget,
  selectedTargetKey,
  onSelect,
}: {
  readonly scopes: readonly RepositoryView[];
  readonly connectorsByTarget: ReadonlyMap<string, readonly AccessSurface[]>;
  readonly selectedTargetKey: string | undefined;
  readonly onSelect: (id: string) => void;
}) {
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<ScopeFilter>('all');
  const [filterOpen, setFilterOpen] = useState(false);

  const filteredScopes = useMemo(() => {
    const q = query.trim().toLowerCase();
    return scopes.filter((scope) => {
      const connectors = connectorsByTarget.get(repositoryViewKey(scope)) ?? [];
      const active = connectors.some(isConnectorActive);
      if (filter === 'active' && !active) return false;
      if (filter === 'inactive' && active) return false;
      if (!q) return true;
      const haystack = `${scope.name ?? ''} ${scope.path ?? ''} ${formatScopePath(scope)}`.toLowerCase();
      return haystack.includes(q);
    });
  }, [connectorsByTarget, filter, query, scopes]);

  return (
    <div
      style={{
        width: '100%',
        height: '100%',
        flex: 1,
        minWidth: 0,
        borderRight: '1px solid var(--po-border-subtle)',
        display: 'flex',
        flexDirection: 'column',
        background: 'var(--po-canvas)',
      }}
    >
      <div
        style={{
          borderBottom: `1px solid ${T.cardBorder}`,
          padding: '8px 8px 10px',
          display: 'flex',
          flexDirection: 'column',
        }}
      >
        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 30px', gap: 6 }}>
          <label
            style={{
              height: 30,
              borderRadius: 6,
              border: `1px solid ${T.border}`,
              background: 'var(--po-control)',
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              padding: '0 10px',
              boxSizing: 'border-box',
              minWidth: 0,
            }}
          >
            <SearchGlyph size={14} />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder='Search scopes'
              style={{
                all: 'unset',
                minWidth: 0,
                flex: 1,
                color: T.text1,
                fontFamily: T.fontSans,
                fontSize: 13,
                lineHeight: '16px',
              }}
            />
          </label>
          <div style={{ position: 'relative' }}>
            <button
              type='button'
              aria-label='Filter access'
              aria-expanded={filterOpen}
              onClick={() => setFilterOpen((v) => !v)}
              style={{
                width: 30,
                height: 30,
                borderRadius: 6,
                border: '1px solid transparent',
                background: 'transparent',
                color: filter !== 'all' || filterOpen ? T.text1 : T.text2,
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                cursor: 'pointer',
              }}
            >
              <FilterGlyph size={14} />
            </button>
            {filterOpen ? (
              <div
                style={{
                  position: 'absolute',
                  zIndex: 5,
                  top: 36,
                  right: 0,
                  width: 150,
                  borderRadius: 8,
                  border: `1px solid ${T.cardBorder}`,
                  background: 'var(--po-panel)',
                  boxShadow: '0 8px 24px color-mix(in srgb, var(--po-shadow) 18%, transparent)',
                  padding: 4,
                }}
              >
                {(['all', 'active', 'inactive'] as const).map((item) => (
                  <button
                    key={item}
                    type='button'
                    onClick={() => {
                      setFilter(item);
                      setFilterOpen(false);
                    }}
                    style={{
                      width: '100%',
                      height: 30,
                      border: 'none',
                      borderRadius: 6,
                      background: filter === item ? 'var(--po-selected)' : 'transparent',
                      color: filter === item ? T.text1 : T.text2,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      padding: '0 8px',
                      fontFamily: T.fontSans,
                      fontSize: 12,
                      fontWeight: filter === item ? 600 : 500,
                      cursor: 'pointer',
                    }}
                  >
                    <span>{item === 'all' ? 'All access' : item === 'active' ? 'Active' : 'Inactive'}</span>
                    {filter === item ? <span aria-hidden>✓</span> : null}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      </div>
      <div
        style={{
          flex: 1,
          overflow: 'auto',
          overflowX: 'hidden',
          position: 'relative',
          paddingTop: 6,
        }}
      >
        <div style={{ padding: '0 0 6px 0', position: 'relative', boxSizing: 'border-box' }}>
          {filteredScopes.map((scope) => (
            <ScopeSidebarRow
              key={repositoryViewKey(scope)}
              scope={scope}
              connectors={connectorsByTarget.get(repositoryViewKey(scope)) ?? []}
              isSelected={repositoryViewKey(scope) === selectedTargetKey}
              onClick={() => onSelect(repositoryViewKey(scope))}
            />
          ))}
          {filteredScopes.length === 0 ? (
            <div
              style={{
                padding: '18px 14px',
                color: T.text3,
                fontFamily: T.fontSans,
                fontSize: 12,
                lineHeight: '18px',
              }}
            >
              No matching access.
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function ScopeSidebarRow({
  scope,
  connectors,
  isSelected,
  onClick,
}: {
  readonly scope: RepositoryView;
  readonly connectors: readonly AccessSurface[];
  readonly isSelected: boolean;
  readonly onClick: () => void;
}) {
  const [hovered, setHovered] = useState(false);
  const isWorkspaceWide = scope.target.kind === 'project_root';
  const displayName = isWorkspaceWide
    ? (scope.name || 'Workspace root')
    : (scope.name || scope.path.split('/').filter(Boolean).pop() || scope.path);
  const subPath = formatScopePath(scope);
  const active = connectors.some(isConnectorActive);
  const connectorCount = connectors.length;

  return (
    <div
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        display: 'flex',
        alignItems: 'center',
        margin: '3px 6px',
        minHeight: 50,
        boxSizing: 'border-box',
        borderRadius: 6,
        background: isSelected ? 'var(--po-selected)' : hovered ? 'var(--po-hover)' : 'transparent',
        color: isSelected ? T.text1 : hovered ? T.text1 : T.text2,
        ...SIDEBAR_ROW_TYPOGRAPHY,
        userSelect: 'none',
        transition: 'background 0.1s, color 0.1s',
        cursor: 'pointer',
        position: 'relative',
      }}
      title={`${displayName} · ${subPath}`}
    >
      <div
        style={{
          flex: 1,
          minWidth: 0,
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'center',
          gap: 4,
          height: '100%',
          boxSizing: 'border-box',
          padding: '6px 8px',
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 7,
            width: '100%',
            minWidth: 0,
          }}
        >
          <StatusDot tone={active ? 'success' : 'muted'} />
          <span
            style={{
              flex: 1,
              minWidth: 0,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              fontFamily: T.fontSans,
              fontWeight: isSelected ? 600 : 500,
            }}
          >
            {displayName}
          </span>
        </div>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            width: '100%',
            minWidth: 0,
            paddingLeft: 14,
          }}
        >
          <span
            style={{
              flex: 1,
              minWidth: 0,
              fontSize: 10,
              color: isSelected ? T.text2 : T.text3,
              fontFamily: T.fontMono,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {subPath}
          </span>
          <CountBadge
            value={connectorCount}
            title={`${connectorCount} ${connectorCount === 1 ? 'connector' : 'connectors'}`}
            size="md"
            tone={isSelected ? 'selected' : 'neutral'}
          />
        </div>
      </div>
    </div>
  );
}

function isConnectorActive(connector: AccessSurface): boolean {
  return connector.status === 'active' || connector.status === 'syncing';
}

function formatScopePath(scope: RepositoryView): string {
  if (scope.target.kind === 'project_root') return '/';
  return `/${scope.path}`;
}

const SearchGlyph = ({ size = 15 }: { readonly size?: number }) => (
  <svg width={size} height={size} viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='2' strokeLinecap='round' strokeLinejoin='round' aria-hidden>
    <circle cx='11' cy='11' r='7' />
    <path d='M16.5 16.5 21 21' />
  </svg>
);

const FilterGlyph = ({ size = 15 }: { readonly size?: number }) => (
  <svg width={size} height={size} viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='2' strokeLinecap='round' strokeLinejoin='round' aria-hidden>
    <path d='M4 7h16' />
    <path d='M7 12h10' />
    <path d='M10 17h4' />
  </svg>
);
