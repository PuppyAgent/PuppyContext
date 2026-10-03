'use client';
import { useProjectSession } from '@/features/workspace/session';

import React, { useState } from 'react';
import type { useWorkspaceRouter as useRouter } from '@/features/workspace/navigation';
import { ArrowRight, ArrowLeft, ArrowLeftRight } from 'lucide-react';
import { StatusDot } from '@/components/ui/StatusDot';
import { CountBadge } from '@/components/ui/CountBadge';
import { getAccessProviderLabel, isGitRemoteProvider } from '@/lib/accessProviderRegistry';
import { T } from '../lib/tokens';
import { getApDirection } from '../lib/constants';
import { dashboardResourceKey, type DashboardEntrypoint } from '../lib/types';
import { ProviderAvatar } from './ProviderAvatar';
import { canonicalProjectGitUrl, canonicalScopeGitUrl } from '@/lib/gitRemote';

// Normalize the three known legacy Project-root path representations the
// backend may emit ('/' or '' for root, null for incomplete rows) into a single
// canonical key.  Used both for the lookup key in `accessByPath` and
// for the hover-sync key here.  Keep this in step with the same
// normalization in page.tsx — if they drift, the chip↔card hover
// handshake silently breaks.
function normalizeApPath(raw: string | null | undefined): string {
  if (raw === null || raw === undefined || raw === '' || raw === '/') {
    return '';
  }
  return raw;
}

// AccessPointsListCard — left-column primary surface that replaces the
// previous ConnectionsCanvas slot.
//
// Layout: section card with the same chrome as Data / History.  Body
// is a flat list (sidebar AccessPointsCard rhythm verbatim: avatar +
// name + status dot + direction + scope path) with URL / `$` rows
// nested below each, indented to align with the AP name.  Adjacent
// APs are separated by a 1px hairline divider so multi-AP lists
// don't visually fuse.
//
// Hover-sync: each AP row + the matching ApChip in the Data tree
// share a single `hoveredPath` source of truth lifted to page.tsx.
// Mousing over either side flips on a highlight on the other:
//   ─ Self-hover (cursor on the AP row) → neutral grey wash
//     (T.rowHover), the same affordance every clickable row in the
//     app uses.  Cyan would over-promise here; the user is actively
//     interacting with the row, not observing a relationship.
//   ─ Synced highlight (chip in the tree above is hovered) → cyan
//     wash (T.rowHighlight), matching the chip pill's accent.  This
//     is the "we're the same thing" state — colour locked to the
//     chip side of the handshake.
//   ─ Both at once: self-hover wins; we don't double-paint.

function DirectionGlyph({ direction }: { direction: 'inbound' | 'outbound' | 'bidirectional' | null }) {
  if (!direction) return null;
  // All three glyphs render in the neutral T.text3 grey.  The earlier
  // version used T.live cyan for outbound + bidirectional with the
  // intent of "outbound is a live wire", but that intent collided
  // with the page-wide rule that cyan is reserved for active live-
  // data signals.  Direction is *metadata* (which way data flows
  // when this AP is used), not a status — the colour was over-
  // promising and contributed to the "everything is cyan" feel.
  // Status freshness is communicated by the standard dot next to the
  // AP name, so size / color mapping stays consistent product-wide.
  const Icon =
    direction === 'outbound'
      ? ArrowLeft
      : direction === 'bidirectional'
        ? ArrowLeftRight
        : ArrowRight;
  return <Icon size={11} strokeWidth={2} style={{ color: T.text3, flexShrink: 0 }} />;
}

// Compose the public-facing endpoint URL for an AP, picking the right
// path shape per provider.  The host comes from NEXT_PUBLIC_API_URL
// when set (build-time-baked backend host) and falls back to the
// current page origin only when no env was provided — same pattern as
// SyncDetailView / GetStartedPanel.
function buildEndpointUrl(conn: DashboardEntrypoint, projectId: string): string | null {
  if (conn.resource_kind !== 'access' || conn.target.project_id !== projectId) return null;
  const apiBase =
    typeof window !== 'undefined'
      ? process.env.NEXT_PUBLIC_API_URL || window.location.origin
      : '';

  switch (conn.kind) {
    case 'git_remote':
      return conn.target.kind === 'project_root'
        ? canonicalProjectGitUrl(apiBase, projectId)
        : canonicalScopeGitUrl(apiBase, projectId, conn.target.scope_id);
    case 'mcp':
    case 'agent':
      return `${apiBase}/api/v1/mcp/proxy`;
    case 'sandbox':
      // Runtime configuration belongs to Access detail, not this summary.
      return null;
    default:
      return null;
  }
}

// Shell command users would actually paste, scoped per provider.
// `null` means we don't render a command row for this provider.
// Non-Git invocation shapes (MCP server config blob, sandbox exec
// body, etc.) live on the /access detail page.
function buildCliCommand(conn: DashboardEntrypoint, url: string | null): string | null {
  if (!url || conn.resource_kind !== 'access' || conn.kind !== 'git_remote') return null;
  // Stock git clone is the canonical setup. The separately issued password
  // is supplied through an OS-backed, path-aware Git credential helper.
  return `git clone ${url}`;
}

// Single-line "label + Copy" row.  Earlier versions also rendered
// the monospaced value below — but at 280px column width, a 50-char
// access_key wraps to 3-4 lines and the AP card balloons vertically
// without delivering any useful read.  The user pastes the value
// into a terminal / config editor; they don't need to *see* it
// inline.  The label ("URL" / "SETUP") already tells them what
// they're copying, and the full string is on the /access detail
// page if they ever want to inspect it.
//
// Net effect: an AP card with URL + SETUP rows is now ~80px tall
// instead of ~280px, which is what 4 separate APs would have looked
// like stacked.  Multi-AP scrolls reasonably; single-AP doesn't
// dwarf the History card below it.
function CopyableLine({
  label,
  value,
  copyKey,
  copiedKey,
  onCopy,
}: {
  label: string;
  value: string;
  copyKey: string;
  copiedKey: string | null;
  onCopy: (text: string, key: string) => void;
}) {
  const isCopied = copiedKey === copyKey;
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: 8,
      }}
    >
      <span
        style={{
          fontSize: 10,
          fontWeight: 600,
          color: T.text3,
          letterSpacing: '0.05em',
          textTransform: 'uppercase',
          fontFamily: T.fontMono,
        }}
      >
        {label}
      </span>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onCopy(value, copyKey);
        }}
        style={{
          flexShrink: 0,
          height: 30,
          padding: '0 10px',
          fontSize: 10,
          fontWeight: 500,
          color: isCopied ? T.live : T.text3,
          background: 'var(--po-hover)',
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 3,
          cursor: 'pointer',
          fontFamily: T.fontSans,
          transition: `color 160ms ${T.ease}, background 160ms ${T.ease}`,
        }}
        onMouseEnter={(e) => {
          if (isCopied) return;
          e.currentTarget.style.background = 'var(--po-border)';
          e.currentTarget.style.color = T.text1;
        }}
        onMouseLeave={(e) => {
          if (isCopied) return;
          e.currentTarget.style.background = 'var(--po-hover)';
          e.currentTarget.style.color = T.text3;
        }}
      >
        {isCopied ? 'Copied' : 'Copy'}
      </button>
    </div>
  );
}

export function AccessPointsListCard({
  projectId,
  router,
  connections,
  hoveredPath,
  onHoverPath,
}: {
  projectId: string;
  router: ReturnType<typeof useRouter>;
  connections: DashboardEntrypoint[];
  hoveredPath: string | null;
  onHoverPath: (path: string | null) => void;
}) {
  const total = connections.length;
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  const handleCopy = (text: string, key: string) => {
    void navigator.clipboard.writeText(text);
    setCopiedKey(key);
    window.setTimeout(() => setCopiedKey(null), 1500);
  };

  return (
    <div
      style={{
        background: T.sectionBg,
        border: `2px solid ${T.sectionBorder}`,
        borderRadius: T.sectionRadius,
        overflow: 'hidden',
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '10px 14px',
          background: T.sectionHeaderBg,
          borderBottom: `1px solid ${T.sectionDivider}`,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 13, fontWeight: 500, color: T.text2 }}>
            Workflows
          </span>
          <CountBadge
            value={total}
            size="md"
            tone={total > 0 ? 'neutral' : 'muted'}
          />
        </div>
      </div>

      <div style={{ padding: '6px 8px', minHeight: 60 }}>
        {total === 0 ? (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              padding: '32px 8px',
              color: T.text3,
              fontSize: 12,
            }}
          >
            No workflows configured.
          </div>
        ) : (
          // Flex column with generous vertical gap rather than 1px
          // dividers.  When every AP is its own card-shaped row
          // (with bg/padding when interactive), the gap between
          // them naturally communicates "these are independent
          // items" without an explicit hairline.  Hairline + bg
          // both fighting for the "row separator" job was making
          // the list look noisy.
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {connections.map((conn) => (
              <ApListRow
                key={dashboardResourceKey(conn)}
                conn={conn}
                projectId={projectId}
                router={router}
                hoveredPath={hoveredPath}
                onHoverPath={onHoverPath}
                copiedKey={copiedKey}
                onCopy={handleCopy}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// Single AP row — hoisted out of the .map so we can keep an
// `isMouseOver` local state that distinguishes "the user actually
// has the cursor on this row" from "this row got highlighted because
// the matching chip in the tree above is hovered".  The two states
// look different by design (see component-level comment).
function ApListRow({
  conn,
  projectId,
  router,
  hoveredPath,
  onHoverPath,
  copiedKey,
  onCopy,
}: {
  conn: DashboardEntrypoint;
  projectId: string;
  router: ReturnType<typeof useRouter>;
  hoveredPath: string | null;
  onHoverPath: (path: string | null) => void;
  copiedKey: string | null;
  onCopy: (text: string, key: string) => void;
}) {
  const openPanel = useProjectSession(state => state.openPanel);
  const [isMouseOver, setIsMouseOver] = useState(false);

  const direction = getApDirection(conn);
  const label = conn.name || getAccessProviderLabel(conn.displayProvider);
  const rawPath = conn.path;
  const isRoot = rawPath === null || rawPath === '' || rawPath === '/';
  const displayScope = isRoot ? '/' : `/${rawPath}`;
  const url = buildEndpointUrl(conn, projectId);
  const cmd = buildCliCommand(conn, url);

  const apPath = normalizeApPath(rawPath);
  const isSyncHighlighted =
    !isMouseOver && hoveredPath !== null && hoveredPath === apPath;

  // Background priority: self-hover (grey, "you can click me") wins
  // over sync (cyan, "we're the same thing as the hovered chip"),
  // wins over rest (transparent).
  const background = isMouseOver
    ? T.rowHover
    : isSyncHighlighted
      ? T.rowHighlight
      : 'transparent';

  return (
    <React.Fragment>
      <div
        onMouseEnter={() => {
          setIsMouseOver(true);
          onHoverPath(apPath);
        }}
        onMouseLeave={() => {
          setIsMouseOver(false);
          onHoverPath(null);
        }}
        style={{
          display: 'flex',
          flexDirection: 'column',
          padding: '10px 10px',
          borderRadius: 6,
          background,
          transition: `background 160ms ${T.ease}`,
        }}
      >
        {/* Identity row — used to be a clickable button that
            navigated to /access?ap=<id>, but that meant any
            mis-click on the avatar / name / scope path while
            hovering Copy buttons below would yank the user off
            the page mid-copy.  Now a plain div: nothing on the
            row navigates by accident.  The explicit "Open" link
            on the right is the single, deliberate management
            entry point. */}
        <div
          title={`${label} — ${displayScope} (${conn.status})`}
          style={{
            padding: '4px 0',
            width: '100%',
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            color: T.text2,
            fontFamily: T.fontSans,
          }}
        >
          {/* Provider avatar — 30 (was 32) to fit the 280px sidebar
              width without crowding the name + Open button on the
              same row.  Icon stays 18 (was 20) for the same reason.
              Avatar shape is still the primary multi-AP
              differentiator, but at this scale the name does more
              of the work and the avatar plays a supporting role. */}
          <div
            style={{
              width: 30,
              height: 30,
              borderRadius: 7,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              background: 'var(--po-hover)',
              flexShrink: 0,
            }}
          >
            <ProviderAvatar
              provider={conn.displayProvider}
              size={18}
              icon={(conn as any).icon}
            />
          </div>

          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              minWidth: 0,
              flexShrink: 1,
            }}
          >
            <span
              style={{
                // Name is the visual primary now (was a 13/500
                // tertiary detail).  Multi-AP scan-the-list use
                // case wants the name to be the column to read
                // top-to-bottom, not the avatar or the URL row.
                fontSize: 14,
                fontWeight: 600,
                color: T.text1,
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
            >
              {label}
            </span>
            <StatusDot status={conn.status} />
          </div>

          {/* Direction + scope — pinned to the right, with scope
              capped at 90px so a long path can't push the Open
              button off-screen.  Truncates with ellipsis when
              that happens; the full path is still readable in
              the row's `title` attribute (browser tooltip on
              hover). */}
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 4,
              marginLeft: 'auto',
              minWidth: 0,
              flexShrink: 1,
            }}
          >
            <DirectionGlyph direction={direction} />
            <span
              style={{
                fontFamily: T.fontMono,
                fontSize: 11,
                color: T.text3,
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                maxWidth: 90,
              }}
            >
              {displayScope}
            </span>
          </div>

          {/* Open — chevron-only at this width.  At 280px we don't
              have horizontal budget for both the AP identity
              (avatar + name + status + scope) AND a labelled "Open"
              button.  The chevron alone reads as "go further" and
              the title attribute provides the affordance text for
              accessibility tooling.  Keeps the deliberate-only
              navigation handle the previous "Open" labelled
              button established. */}
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              openPanel(conn.resource_kind === 'synchronize'
                ? { type: 'sync_config', synchronizeBindingId: conn.resource_id, nodeId: conn.path }
                : { type: 'access_list', view: 'detail', accessEndpointId: conn.resource_id });
            }}
            title="Open integration details"
            aria-label="Open integration details"
            style={{
              flexShrink: 0,
              marginLeft: 4,
              background: 'none',
              border: 'none',
              cursor: 'pointer',
              width: 30,
              height: 30,
              padding: 0,
              color: T.text3,
              display: 'inline-flex',
              alignItems: 'center',
              borderRadius: 4,
              transition: `color 160ms ${T.ease}, background 160ms ${T.ease}`,
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.color = T.text1;
              e.currentTarget.style.background = 'var(--po-border-subtle)';
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.color = T.text3;
              e.currentTarget.style.background = 'transparent';
            }}
          >
            <svg width="10" height="10" viewBox="0 0 12 12" fill="none">
              <path
                d="M4 2l4 4-4 4"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        </div>

        {/* Endpoint detail rows.  Drawn flush-left with the AP
            row's own padding (no 42px indent) so they read as
            primary content of this card, not a hanging detail
            block.  A 1px hairline along the top separates them
            from the identity row above — the divider is the
            structural cue ("identity vs endpoint info") that the
            old indent was trying and failing to communicate. */}
        {(url || cmd) && (
          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              gap: 6,
              marginTop: 10,
              paddingTop: 10,
              borderTop: `1px solid ${T.cardBorder}`,
            }}
          >
            {url && (
              <CopyableLine
                label="URL"
                value={url}
                copyKey={`url-${dashboardResourceKey(conn)}`}
                copiedKey={copiedKey}
                onCopy={onCopy}
              />
            )}
            {/* `git clone` is the *one-time setup* command, not a
                command the user runs daily.  Once the local folder
                is connected, day-to-day work happens via local
                `git push` / `git pull` and the user never needs to
                touch the URL again.  The `SETUP` label makes that
                lifecycle explicit so users who already connected
                don't try to re-paste this every time they revisit
                the page.  When the dashboard later surfaces use-
                case-specific commands (Claude / Cursor / Cline
                config snippets), this row's label can become a
                segmented control between them. */}
            {cmd && (
              <CopyableLine
                label="SETUP"
                value={cmd}
                copyKey={`cmd-${dashboardResourceKey(conn)}`}
                copiedKey={copiedKey}
                onCopy={onCopy}
              />
            )}
            {conn.resource_kind === 'access' && isGitRemoteProvider(conn.kind) ? (
              <span style={{ color: T.text3, fontSize: 11, lineHeight: '16px' }}>
                Open this Git Remote to generate its separate one-time password; the URL is not a key.
              </span>
            ) : null}
          </div>
        )}
      </div>

    </React.Fragment>
  );
}
