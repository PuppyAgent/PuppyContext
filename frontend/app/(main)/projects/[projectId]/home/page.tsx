'use client';
import { useProjectSession } from '@/features/workspace/session';

import { use, useMemo, useEffect, useState } from 'react';
import { useWorkspaceRouter as useRouter } from '@/features/workspace/navigation';
import { useOnboarding } from '@/lib/hooks/useOnboarding';
import { getResourceDashboard } from '@/lib/resourceDashboardApi';
import { useAuth } from '@/contexts/SupabaseAuthProvider';
import { dashboardEntrypoint } from './lib/types';
import useSWR from 'swr';
import { treeList, getProjectHistory, sortNodes } from '@/lib/contentTreeApi';

import { T } from './lib/tokens';
import { formatRelative } from './lib/format';
import type {
  ProjectDashboard,
  DashboardEntrypoint,
  TreeNode,
} from './lib/types';

import { TreeRows, type RowVariant } from './components/TreeRows';
import { HistoryCard } from './components/HistoryCard';
import { GetStartedPanel } from './components/GetStartedPanel';
import { ApChip } from './components/ApChip';
import { ProjectPageLoadingShell } from '@/components/loading';
import { ProjectGitHealthBadge } from '@/components/ProjectGitHealthBadge';
import { CountBadge } from '@/components/ui/CountBadge';

// ConnectionsCanvas (the old xyflow wiring board) used to mount here.
// Home now surfaces Access Points directly under the Data card via
// AccessPointsListCard — that's a denser, copy-driven surface that
// replaces the supplementary graph view (which was already collapsed
// by default and rarely opened in practice).  ConnectionsCanvas.tsx
// itself stays in the tree for now in case we want to bring the graph
// back as a toggle on the AP card; this just stops importing it from
// /home, so the bundle that ships for this route no longer pulls in
// xyflow / @xyflow/react.

// Vitals-strip interpunct separator.  A single `·` glyph rendered in
// the faint `text4` token with a small horizontal rhythm so the strip
// reads as one inline sentence ("Active · 59bc58e5 · 6 commits · …")
// rather than five disconnected tokens.  Local helper because the
// strip is the only consumer.
function Sep() {
  return (
    <span
      aria-hidden
      style={{
        margin: '0 8px',
        color: T.text3,
        fontSize: 13,
        lineHeight: 1,
        userSelect: 'none',
      }}
    >
      ·
    </span>
  );
}

// HomePage layout — two structural bands, deliberately not jumbled
// together into a single multitasking surface.
//
//   1.  HEADER          : project identity + at-a-glance vitals.
//                         Title, project ID, status dot, commit
//                         count, AP count, last-updated relative
//                         time.  Just enough to answer "what is this
//                         project, is it healthy, and is anyone
//                         touching it?" — no charts, no commit list,
//                         no AP cards.
//
//   2.  TWO-COLUMN BAND : the project's contents + activity.
//                         LEFT  — Data card (file tree, the
//                                 project's actual payload) STACKED
//                                 over the ConnectionsCanvas (xyflow
//                                 wiring board showing which APs
//                                 attach to which scopes).  Both
//                                 share the wide left column because
//                                 they're two complementary views of
//                                 the same thing — the Data card
//                                 answers "what files exist", the
//                                 canvas answers "and which APs are
//                                 wired to those files".  Stacking
//                                 them keeps the relationship local
//                                 instead of forcing a long visual
//                                 leap to a separate band.
//                         RIGHT — stacked HistoryCard +
//                                 AccessPointsCard.  The right rail
//                                 is the "who cares about this
//                                 project" column: history = past
//                                 activity, APs = active hooks.
//
// The shape mirrors GitHub's repo home (header / contents) — a
// layout users have an eight-year mental model for — while the
// canvas adds the one piece GitHub doesn't have: a manipulable
// system-level view of how data and access points wire together,
// nestled directly under its parent Data card so the relationship
// reads as "supplementary" not "separate band".

export default function HomePage({
  params,
}: {
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = use(params);
  const router = useRouter();
  const { userId, session, isAuthReady } = useAuth();
  const openPanel = useProjectSession(state => state.openPanel);

  // Shared hover key for the Data ApChip ↔ AccessPointsListCard
  // handshake.  When the user mouses over either side, this stores
  // the matching path (project-root chip uses '', a folder/file chip
  // uses its tree path, an AP card uses its `normalizeApPath`-d
  // value).  Both sides then highlight when their own path matches.
  // Lifted to page-level state because the chip and the card live in
  // sibling components — there's no parent below this point that
  // owns both.
  const [hoveredPath, setHoveredPath] = useState<string | null>(null);

  // Data

  const { data: dashboard, error: dashboardError, mutate: mutateDashboard } = useSWR<ProjectDashboard>(
    projectId && userId && isAuthReady ? ['resource-dashboard', userId, projectId] : null,
    () => getResourceDashboard(projectId),
    {
      refreshInterval: 120_000,
      revalidateOnFocus: true,
      revalidateOnReconnect: true,
      dedupingInterval: 5_000,
    },
  );

  useEffect(() => {
    if (userId && isAuthReady) void mutateDashboard();
  }, [userId, isAuthReady, session?.access_token, mutateDashboard]);

  const { data: treeEntries, mutate: mutateTree } = useSWR(
    projectId ? ['home-tree', projectId] : null,
    () => treeList(projectId, '', 3),
    { revalidateOnFocus: false },
  );

  const { data: historyData } = useSWR(
    projectId ? ['project-history-overview', projectId] : null,
    () => getProjectHistory(projectId, 50),
    { revalidateOnFocus: false },
  );

  const commits = historyData?.commits || [];
  const latestCommit = commits.length > 0 ? commits[commits.length - 1] : null;

  // 30-day commit cadence for the HistoryCard sparkline.  Bucket dates
  // are right-anchored at today so the sparkline always reads "what's
  // happened in the last month, ending now".
  const commitBuckets = useMemo(() => {
    const buckets: { date: string; count: number }[] = [];
    const now = new Date();
    for (let i = 29; i >= 0; i--) {
      const d = new Date(now);
      d.setDate(d.getDate() - i);
      buckets.push({ date: d.toISOString().slice(0, 10), count: 0 });
    }
    commits.forEach((c) => {
      if (!c.created_at) return;
      const day = c.created_at.slice(0, 10);
      const bucket = buckets.find((b) => b.date === day);
      if (bucket) bucket.count++;
    });
    return buckets;
  }, [commits]);

  const connections = useMemo<DashboardEntrypoint[]>(() => {
    return dashboard?.resources.map(dashboardEntrypoint) || [];
  }, [dashboard]);

  // Auto-complete onboarding steps based on real data
  const { completeStep } = useOnboarding();
  useEffect(() => {
    if ((dashboard?.nodes?.total ?? 0) > 0) completeStep('file');
    if (connections.length > 0) completeStep('access_point');
  }, [dashboard, connections.length]); // eslint-disable-line react-hooks/exhaustive-deps

  // Tree shaped from the flat treeList response.  Order comes from the
  // shared `sortNodes` helper so Home and Data show the same arrangement
  // and any future tweak to the canonical order (mtime, type-priority,
  // user preference, …) lives in one place.
  const tree = useMemo<TreeNode[]>(() => {
    const entries = treeEntries || [];
    const sorted = sortNodes(entries);

    const nodeMap = new Map<string, TreeNode>();
    const roots: TreeNode[] = [];

    for (const entry of sorted) {
      nodeMap.set(entry.path, { entry, children: [] });
    }
    for (const entry of sorted) {
      const node = nodeMap.get(entry.path)!;
      const slashIdx = entry.path.lastIndexOf('/');
      if (slashIdx === -1) {
        roots.push(node);
      } else {
        const parentPath = entry.path.substring(0, slashIdx);
        const parent = nodeMap.get(parentPath);
        if (parent) parent.children.push(node);
        else roots.push(node);
      }
    }
    return roots;
  }, [treeEntries]);

  // path → APs that target it.  TreeRows uses this to render the soft
  // `T.rowAttached` whisper-tint on rows that any AP touches, so the
  // tree itself silently flags "external systems care about this row"
  // even when nothing is hovered.  No cross-section hover sync (hover
  // an AP → cyan-band the tree) in this layout — APs and the tree
  // live in different bands now, so a coordinated highlight would
  // require a long visual leap that the TopologyCanvas already
  // serves more directly.
  const accessByPath = useMemo(() => {
    const map = new Map<string, DashboardEntrypoint[]>();
    for (const conn of connections) {
      // Normalize the three legacy Project-root path representations the
      // backend can produce into a single key — '' — so downstream
      // consumers only have to look in one place:
      //
      //   path === '/'   — root access surfaces may serialize this way
      //   path === null  — incomplete rows from before path-NOT-NULL was
      //                    enforced; still in some long-lived projects
      //   path === ''    — early hand-bootstrapped rows
      //
      // Without this, the Project-root TreeRow's ApChip lookup
      // (`accessByPath.get('')`) misses the AP entirely because its
      // path was '/' under the previous `conn.path || ''` pass-through
      // (truthy → key stays '/'), so the chip silently doesn't render
      // even though a real root-scope AP is wired.
      const raw = conn.path;
      const key = raw === null || raw === '' || raw === '/' ? '' : raw;
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(conn);
    }
    return map;
  }, [connections]);

  // Data card view — every top-level entry, plus a 1-level preview
  // of each top-level folder's children, capped to a soft total
  // budget so the card doesn't grow unbounded on dense projects.
  //
  // Knob choices, with rationale:
  //   ─ Depth = 1.   Two reasons.  (a) anything deeper duplicates
  //                  the data explorer + ConnectionsCanvas below
  //                  (which exists specifically for relational
  //                  drill-down).  (b) 1 level is the minimum that
  //                  meaningfully answers "what's actually inside
  //                  this folder?" without becoming a tree browser.
  //
  //   ─ Per-folder cap = 6.  Sized to the screenshot case (a folder
  //                  with exactly 6 children) so it shows ALL of
  //                  them, no awkward "… 1 more" tail.  Past 6 we
  //                  collapse to 6 + "… N more" — same placeholder
  //                  mechanism ConnectionsCanvas uses for sibling
  //                  sampling, so users see the same visual
  //                  contract twice on the page.
  //
  //   ─ Total cap = 14 rows.  At ROW_HEIGHT 32 that's ~448px,
  //                  comfortably above the card's 280px minHeight
  //                  but well short of the page becoming a wall of
  //                  tree.  Treated as a budget (see allocation
  //                  below) — not a hard truncation.
  //
  //   ─ Top-level rows are NEVER truncated.  The Data card's job
  //                  is "what's in this project at a glance"; the
  //                  one thing it must not do is silently hide a
  //                  top-level entry.  If a project has 50
  //                  top-level entries, the card grows to 50 rows
  //                  and the page-level scroller takes over — the
  //                  cap only governs how much we expand.
  //
  //   ─ Budget walks top-down.  Top-level folders earlier in the
  //                  tree get their full 6-child preview first;
  //                  later folders take whatever's left.  This
  //                  mirrors how readers consume top-down — the
  //                  first folder is what their eye lands on, so a
  //                  rich preview there carries the most weight,
  //                  and a thin or no preview later just leaves
  //                  those folders as collapsed rows (still
  //                  clickable to drill in).
  //
  //   ─ Grandchildren stay hidden.  Folders previewed under a top-
  //                  level folder render as collapsed rows (children
  //                  = []) and don't get their own count chip — the
  //                  count would invite the eye to drill, which is
  //                  the explorer's job.  Top-level folders DO keep
  //                  their count chip ("New Folder · 6") because
  //                  it's the GitHub-style "this folder contains 6
  //                  things" cue users expect.
  const dataCardView = useMemo<{
    tree: TreeNode[];
    variants: Map<string, RowVariant>;
  }>(() => {
    const CHILDREN_PER_FOLDER = 6;
    const SOFT_TOTAL_CAP = 14;
    const variants = new Map<string, RowVariant>();

    // Seed every top-level entry as a collapsed row first, so the
    // baseline "what's in this project" surface is always present
    // regardless of how the expansion budget shakes out.
    const result = tree.map<TreeNode>((top) => ({
      entry: top.entry,
      children: [],
    }));
    let used = result.length;

    // Walk top-level folders in order, expanding each within
    // whatever budget remains.  Once budget is exhausted, all
    // subsequent folders stay collapsed.
    for (let i = 0; i < tree.length; i++) {
      const top = tree[i];
      if (top.entry.type !== 'folder' || top.children.length === 0) {
        continue;
      }

      const budget = SOFT_TOTAL_CAP - used;
      if (budget <= 0) break;

      // Natural plan, ignoring the budget: show min(K, cap)
      // children, plus a "… N more" placeholder if K > cap.
      const naturalShown = Math.min(top.children.length, CHILDREN_PER_FOLDER);
      const naturalPlaceholder = top.children.length > naturalShown;
      const naturalRows = naturalShown + (naturalPlaceholder ? 1 : 0);

      let numChildren: number;
      let hasPlaceholder: boolean;

      if (naturalRows <= budget) {
        numChildren = naturalShown;
        hasPlaceholder = naturalPlaceholder;
      } else if (budget >= 2) {
        // Budget too tight for the natural plan but at least 2 rows
        // available — reserve 1 for the placeholder so the user
        // still sees "this folder has more, click to drill in".
        numChildren = budget - 1;
        hasPlaceholder = top.children.length - numChildren > 0;
      } else {
        // Budget = 1.  Prefer surfacing one real child over a lone
        // placeholder — at least the user sees an example of what
        // lives inside.
        numChildren = 1;
        hasPlaceholder = false;
      }

      const sampled = top.children.slice(0, numChildren);
      const childNodes: TreeNode[] = sampled.map<TreeNode>((c) => ({
        // Strip grandchild count so previewed sub-rows don't
        // suggest "you can drill in here" — we deliberately don't
        // let the user expand level-2 from the Data card.
        entry: { ...c.entry, children_count: null },
        children: [],
      }));

      if (hasPlaceholder) {
        const remaining = top.children.length - numChildren;
        const placeholderPath = `__more__:${top.entry.path}`;
        variants.set(placeholderPath, 'placeholder');
        childNodes.push({
          entry: {
            name: `… and ${remaining} more`,
            path: placeholderPath,
            type: 'file',
            content_hash: null,
            size_bytes: 0,
            mime_type: null,
            children_count: null,
          },
          children: [],
        });
      }

      result[i] = { entry: top.entry, children: childNodes };
      used += childNodes.length;
    }

    return { tree: result, variants };
  }, [tree]);

  // ── Render ───────────────────────────────────────────────────────

  // Keep the page header band mounted during the initial data load so
  // the loader is centered in the same body region as the final page.
  if (dashboardError) {
    return <div role="alert" style={{ padding: 24 }}>
      Failed to load project resources. <button type="button" onClick={() => void mutateDashboard()}>Retry</button>
    </div>;
  }
  if (!dashboard || dashboard.project.id !== projectId) {
    return <ProjectPageLoadingShell title="Home" />;
  }

  const hasErr = connections.some((c) => c.status === 'error');

  return (
    // No `background` here — `(main)/layout.tsx` already paints the
    // rounded var(--po-canvas) pane.  Painting again would (a) cover the corner
    // radius, (b) drift if the layout's surface color ever changes.
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        color: T.text2,
        fontFamily: T.fontSans,
      }}
    >
      {/* Top bar — minimal label + hairline divider, matches every
          other top-level page in /(main). */}
      <div
        style={{
          height: 46,
          minHeight: 46,
          flexShrink: 0,
          display: 'flex',
          alignItems: 'center',
          padding: '0 20px',
          borderBottom: `1px solid ${T.border}`,
          fontSize: 12,
          fontWeight: 500,
          color: T.text2,
          letterSpacing: '0.01em',
        }}
      >
        <span>Home</span>
      </div>

      <div style={{ flex: 1, overflowY: 'auto' }}>
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            // 1200 (was 1080) gives the Data card more breathing room
            // while keeping line lengths readable.  Matches the
            // GitHub-style reference's overall horizontal spread.
            maxWidth: 1200,
            margin: '0 auto',
            width: '100%',
            // Title sits ~32px below the top bar (was 64px).  Reference
            // anchors the title close to the top, not centered in
            // a "poster" margin.
            padding: '32px 32px 96px',
          }}
        >
          {/* Surface project-root Git view health when it's NOT
            * ``healthy``. Silent on healthy/empty so the home page
            * stays calm; expands to a colored banner with a "Rebuild
            * cache" action button when current_corrupt is detected.
            * The banner reads from the JWT-authenticated Project Git-view
            * control plane every 60s. The /git data plane remains reserved
            * for scope-bounded runtime credentials.
            */}
          <ProjectGitHealthBadge projectId={projectId} />
          {/* ============================================================
              BAND 1 — HEADER.  Pixel-borrowed from the OLD GitHub-style
              page so users carry over their existing mental model:
                Row 1   : title (compact 28px, top-anchored — not a
                          poster-sized headline)
                Row 2   : vitals strip — status dot + Active, short
                          commit hash, N commits, N access points, last
                          updated relative time, all separated by `·`
                          interpuncts (not the wide 28px column gaps the
                          previous draft used)
                Row 3   : full project UUID + copy button on its own
                          line, mono + faint — visually subordinate to
                          the title and vitals because most users will
                          glance at it once for `cli login --project=…`
                          and never look again
              No green Connect button (the old version had one, but
              project-level "Connect" is a misleading affordance: the
              actual connect-flow always targets a specific access
              point, never the project root).
              ============================================================ */}

          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              gap: 6,
              minWidth: 0,
            }}
          >
            {/* Row 1 — title.  24px, weight 600, tight letter-spacing.
                Trimmed from 28px because the previous size read as
                "page poster" rather than the GitHub-style "this is
                the project, here's its identity strip below" the
                reference uses.  No inline ID alongside it — the ID
                gets its own row below the vitals. */}
            <h1
              style={{
                fontSize: 24,
                fontWeight: 600,
                letterSpacing: '-0.015em',
                color: T.text1,
                margin: 0,
                lineHeight: 1.2,
              }}
            >
              {dashboard.project.name}
            </h1>

            {/* Row 2 — vitals strip.  `·` interpuncts between cells
                (not column-gap whitespace) so the strip reads as a
                single inline sentence.  All cells share one font
                size (13px) so the strip reads as a coherent line of
                metadata rather than a jumble of mismatched type. */}
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                flexWrap: 'wrap',
                rowGap: 4,
                fontSize: 13,
                color: T.text3,
                marginTop: 2,
              }}
            >
              <span
                style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}
              >
                <span
                  aria-hidden
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: '50%',
                    background: hasErr ? T.err : T.live,
                    boxShadow: hasErr ? 'none' : `0 0 0 3px ${T.liveSoft}`,
                  }}
                />
                <span style={{ color: T.text2 }}>
                  {hasErr ? 'Unhealthy' : 'Active'}
                </span>
              </span>

              {/* Short commit hash — same idea as `git log --oneline`'s
                  abbreviated SHA.  Falls back to "—" when there is
                  no history yet so the strip's rhythm doesn't
                  collapse to two cells. */}
              <Sep />
              <span
                style={{
                  fontFamily: T.fontMono,
                  fontSize: 13,
                  color: latestCommit ? T.text2 : T.text3,
                  fontVariantNumeric: 'tabular-nums',
                }}
                title={latestCommit?.commit_id || 'No commits yet'}
              >
                {latestCommit?.commit_id?.slice(0, 8) ?? '—'}
              </span>

              <Sep />
              <button
                onClick={() => router.push(`/projects/${projectId}/changes`)}
                style={{
                  background: 'none',
                  border: 'none',
                  height: 30,
                  padding: 0,
                  cursor: 'pointer',
                  fontFamily: T.fontSans,
                  color: T.text3,
                  fontSize: 13,
                  display: 'inline-flex',
                  alignItems: 'baseline',
                  gap: 4,
                }}
              >
                <span
                  style={{
                    color: commits.length > 0 ? T.text2 : T.text3,
                    fontVariantNumeric: 'tabular-nums',
                    fontWeight: 500,
                  }}
                >
                  {commits.length}
                </span>
                <span>
                  {commits.length === 1 ? 'commit' : 'commits'}
                </span>
              </button>

              <Sep />
              <button
                onClick={() => openPanel({ type: 'access_list', view: 'overview' })}
                style={{
                  background: 'none',
                  border: 'none',
                  height: 30,
                  padding: 0,
                  cursor: 'pointer',
                  fontFamily: T.fontSans,
                  color: T.text3,
                  fontSize: 13,
                  display: 'inline-flex',
                  alignItems: 'baseline',
                  gap: 4,
                }}
              >
                <span
                  style={{
                    color: connections.length > 0 ? T.text2 : T.text3,
                    fontVariantNumeric: 'tabular-nums',
                    fontWeight: 500,
                  }}
                >
                  {connections.length}
                </span>
                <span>
                  access {connections.length === 1 ? 'point' : 'points'}
                </span>
              </button>

              {latestCommit && (
                <>
                  <Sep />
                  <span>
                    {formatRelative(latestCommit.created_at)}
                  </span>
                </>
              )}
            </div>

            {/* Row 3 — full project UUID with copy button, mono and
                faint.  Visually demoted vs the title + vitals so it
                doesn't compete for attention but stays glanceable
                when a user needs the literal ID for the CLI.  12px
                (was 11px) so it sits one notch below the vitals
                strip without dropping into "footnote" territory. */}
            <div
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                marginTop: 4,
                fontFamily: T.fontMono,
                fontSize: 12,
                color: T.text3,
              }}
            >
              <span style={{ userSelect: 'all' }}>{projectId}</span>
              <button
                onClick={() => navigator.clipboard.writeText(projectId)}
                style={{
                  background: 'none',
                  border: 'none',
                  color: T.text3,
                  cursor: 'pointer',
                  width: 30,
                  height: 30,
                  padding: 0,
                  display: 'flex',
                  alignItems: 'center',
                  transition: `color 200ms ${T.ease}`,
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.color = T.text1;
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.color = T.text3;
                }}
                title="Copy project ID"
              >
                <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor">
                  <path d="M0 6.75C0 5.784.784 5 1.75 5h1.5a.75.75 0 0 1 0 1.5h-1.5a.25.25 0 0 0-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 0 0 .25-.25v-1.5a.75.75 0 0 1 1.5 0v1.5A1.75 1.75 0 0 1 9.25 16h-7.5A1.75 1.75 0 0 1 0 14.25v-7.5Z" />
                  <path d="M5 1.75C5 .784 5.784 0 6.75 0h7.5C15.216 0 16 .784 16 1.75v7.5A1.75 1.75 0 0 1 14.25 11h-7.5A1.75 1.75 0 0 1 5 9.25v-7.5Zm1.75-.25a.25.25 0 0 0-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 0 0 .25-.25v-7.5a.25.25 0 0 0-.25-.25h-7.5Z" />
                </svg>
              </button>
            </div>
          </div>

          {/* ============================================================
              EMPTY-STATE BRANCH.  Trigger is "no DATA" — we deliberately
              do NOT include `connections.length === 0` in the test.
              Reason: the CLI flow creates an access point BEFORE any
              files arrive (sometimes minutes-to-hours before, while the
              user configures `git`, runs `git clone`, edits files, and
              eventually `git push`es).  The previous condition collapsed
              the panel the instant the AP was minted, ripping the very
              `git clone …` command out from under the user — onboarding
              failing at the moment of perceived success.

              "Project is empty in any user-meaningful sense" = nodes
              total is 0.  An AP without data behind it is setup-in-
              progress, not completion.  The panel sticks around until
              actual content lands (drop → upload, or `git push` → sync),
              then retires automatically via SWR revalidation.

              Connections are passed in so the Git card can locate the
              existing root Git Remote surface. Credentials are issued
              explicitly and shown once; dashboard lists never return them.
              ============================================================ */}

          {(dashboard?.nodes?.total ?? 0) === 0 ? (
            <GetStartedPanel
              projectId={projectId}
              connections={connections}
              onChanged={() => {
                void mutateDashboard();
                void mutateTree();
              }}
            />
          ) : (
            <>
          {/* ============================================================
              BAND 2 — TWO-COLUMN.
                LEFT  (flex 1) — Data card.  Just the file tree;
                                the AP / wiring view used to live
                                here too but moved to the right rail
                                so the page reads left → right as
                                "data → external connections" (data
                                is the project's *content*, AP is
                                its external *exposure*; spatially
                                pairing them across columns is the
                                most direct visual representation of
                                that "data flows out via these
                                endpoints" relationship).
                RIGHT (280px) — Access Points + History stacked.
                                Access Points goes first because the
                                user's typical work is "look at
                                Data, then act on it via an AP";
                                History sits below as supporting
                                time-series context.
              32px gap between columns; 16px gap between stacked
              cards within each column.
              ============================================================ */}

          <div
            style={{
              display: 'flex',
              // 32px gap (was 24) gives the right rail a clear breathing
              // room from the Data card — at 24px the two sections
              // visually fused into one wide block.
              gap: 32,
              alignItems: 'flex-start',
              marginTop: 48,
            }}
          >
            {/* LEFT COLUMN — Data card stacked over the
                ConnectionsCanvas.  Both share this column because
                they're complementary views of the same thing: the
                Data card answers "what files exist?", the canvas
                answers "and which APs are wired to them?".  16px
                gap matches the right rail's stack rhythm. */}
            <div
              style={{
                flex: 1,
                minWidth: 0,
                display: 'flex',
                flexDirection: 'column',
                // Wider vertical gap between the Data card and the
                // Connections canvas below.  16 read as "stacked
                // siblings" — same band, two rows; 24 reads as
                // "primary card, then supplementary band" which is
                // the actual hierarchy: Data IS the page, Connections
                // is a relational annotation hanging off the bottom.
                gap: 24,
              }}
            >
              {/* Data card — inlined here because (a) it composes a
                  fairly local use of TreeRows + accessByPath + the
                  existing project store, and (b) pulling it into
                  its own file would be more indirection than the
                  ~50 lines justify. */}
            <div
              style={{
                background: T.sectionBg,
                border: `2px solid ${T.sectionBorder}`,
                borderRadius: T.sectionRadius,
                overflow: 'hidden',
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
                  {/* Title-case label, no letter-spacing.  The previous
                      `DATA` (uppercase 11px letter-spaced) read as a
                      "tech enterprise dashboard" header; the GitHub
                      reference uses normal-case 13px which feels like
                      a sentence-fragment tag instead. */}
                  <span
                    style={{
                      fontSize: 13,
                      fontWeight: 500,
                      color: T.text2,
                    }}
                  >
                    Data
                  </span>
                  {dashboard?.nodes?.total != null && (
                    <CountBadge
                      value={dashboard.nodes.total}
                      size="md"
                      tone="neutral"
                    />
                  )}
                </div>
                <button
                  onClick={() => router.push(`/projects/${projectId}/data`)}
                  style={{
                    background: 'none',
                    border: 'none',
                    cursor: 'pointer',
                    height: 30,
                    padding: 0,
                    fontSize: 12,
                    color: T.text2,
                    fontFamily: T.fontSans,
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 4,
                    transition: `color 200ms ${T.ease}`,
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.color = T.text1;
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.color = T.text2;
                  }}
                >
                  Browse
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

              {/* `minHeight` so the Data card doesn't visually
                  collapse when the project only has 1-2 top-level
                  entries — even with the 1-level expansion above,
                  some projects (all-files-no-folders) won't pick
                  up extra rows from auto-expand and would still
                  look thin without this floor.  320px ≈ 10 rows
                  at ROW_HEIGHT 32 — enough that the card always
                  reads as the page's primary content well, and
                  in turn lets the Connections canvas below grow
                  to a comparable height without breaking the rule
                  that Connections must stay shorter than Data
                  (see ConnectionsCanvas's height comment).

                  `maxHeight` caps the card so a heavy top-level
                  ("39 .txt files at the project root") doesn't
                  silently dwarf the right rail — the previous
                  design relied on the page-level scroller, which
                  works fine for a 50-entry tree where the right
                  rail goes off-screen with it, but breaks down
                  when the rail is ~300px and Data wants to be
                  ~1200px (the rail flat-lines while Data marches
                  on, breaking the two-column band's read).  480px
                  ≈ 15 rows at ROW_HEIGHT 32 — sized JUST above
                  SOFT_TOTAL_CAP (14 rows ≈ 460px) so the
                  expansion budget still fits naturally without
                  triggering an internal scroller in the common
                  case; only the genuinely-overflowing case (this
                  project, with 39 flat files) scrolls inside the
                  card.  `overflowY: auto` keeps the bar invisible
                  until the content actually overflows. */}
              <div
                style={{
                  padding: '6px 0',
                  minHeight: 320,
                  maxHeight: 480,
                  overflowY: 'auto',
                }}
              >
                {dataCardView.tree.length === 0 ? (
                  <div
                    style={{
                      padding: '64px 0',
                      textAlign: 'center',
                      color: T.text3,
                      fontSize: 13,
                    }}
                  >
                    Empty project
                  </div>
                ) : (
                  // GitHub-style "what's in this repo at a glance" —
                  // every top-level entry shown, plus a 6-child
                  // preview under each top-level folder (with
                  // "… N more" for the rest).  Drilling deeper is
                  // one click on any row, which routes into the
                  // data explorer's recursive view.
                  //
                  // Wrapped in a synthetic ROOT node (`path: ''`,
                  // matching the convention `accessByPath` uses
                  // for project-root APs in ConnectionsCanvas).
                  // Two reasons:
                  //   1.  AP visibility — root-attached access points
                  //       had no anchor row in the Data card, so
                  //       their presence was visible only in the
                  //       Connections canvas below.  Now the chip
                  //       renders directly on the root row, making
                  //       "this whole project is exposed via X APs"
                  //       legible at the top of the tree.
                  //   2.  Visual parity with /data — that page's
                  //       sidebar already renders a "root" entry as
                  //       the parent of all top-level files; mirroring
                  //       that here removes a "where am I?" mismatch
                  //       when users move between Home and Data.
                  //
                  // `renderRowExtras` injects a tiny <ApChip /> on the
                  // right of any row that has APs attached.  Reuses
                  // the slot ConnectionsCanvas already uses for its
                  // xyflow Handles — same protocol, different payload.
                  //
                  // No cross-section hover sync — `highlightedPaths`
                  // is null so TreeRows renders only its quiet rest
                  // state (rowAttached tint where APs touch, no cyan
                  // band).  ConnectionsCanvas below carries the
                  // "which AP touches what" job, freeing the tree
                  // to stay a quiet file listing.
                  <TreeRows
                    nodes={[
                      {
                        entry: {
                          // Synthetic root row name kept in step with
                          // /data's ExplorerSidebar root entry
                          // ("Root").  Two renders of the same
                          // semantic node should read with the same
                          // word — earlier we rendered "Project root"
                          // here while /data renders "Root", which
                          // made users moving between the two pages
                          // wonder if they were the same node.
                          name: 'Root',
                          path: '',
                          type: 'folder',
                          content_hash: null,
                          size_bytes: 0,
                          mime_type: null,
                          children_count: dataCardView.tree.length,
                        },
                        children: dataCardView.tree,
                      },
                    ]}
                    depth={0}
                    projectId={projectId}
                    router={router}
                    accessByPath={accessByPath}
                    // Single-row highlight driven by the shared
                    // hoveredPath.  When the user mouses over the
                    // matching ApChip in this tree OR an AP row in
                    // the AccessPointsListCard below, the whole
                    // matching Data row gets the rest-state cyan
                    // band — not just the chip pill, which would be
                    // too small a target for the user's eye to
                    // catch.  Anchor depth is computed from the
                    // path's own depth (root '' → 0, top-level
                    // 'foo.md' → 1, 'docs/foo.md' → 2 …) so the
                    // band's left edge sits at the right indent
                    // for that row's visual depth.  No descendant
                    // sweep here on purpose: we want exactly one
                    // row to light up, matching the chip↔card
                    // symmetry.
                    highlightedPaths={
                      hoveredPath !== null
                        ? new Set([hoveredPath])
                        : null
                    }
                    highlightAnchorDepth={
                      hoveredPath === null
                        ? -1
                        : hoveredPath === ''
                          ? 0
                          : hoveredPath.split('/').length
                    }
                    rowVariants={dataCardView.variants}
                    renderRowExtras={(path) => {
                      const aps = accessByPath.get(path);
                      return aps && aps.length > 0 ? (
                        <ApChip
                          aps={aps}
                          rowPath={path}
                          hoveredPath={hoveredPath}
                          onHoverPath={setHoveredPath}
                        />
                      ) : null;
                    }}
                  />
                )}
              </div>
            </div>

            </div>

            {/* RIGHT — stacked rail.  Now hosts AccessPointsListCard
                + HistoryCard, in that order.  The reorder is a
                spatial-semantic move: the page reads left → right
                as "data → external connections", so the AP block
                has to sit *to the right of* Data, not below it.
                With AP underneath Data the visual chain was broken
                — the user had to scroll down + back up to map
                which row maps to which AP — and the right rail
                was busy holding History alone, which doesn't need
                that much space.  Now: Data | (AP + History).
                Width was 280 — kept, even though AP pulled in,
                because AccessPointsListCard reflows internally to
                a vertical CopyableLine layout when the parent is
                narrow. */}
            <div
              style={{
                width: 280,
                flexShrink: 0,
                display: 'flex',
                flexDirection: 'column',
                gap: 16,
              }}
            >
              <HistoryCard
                projectId={projectId}
                router={router}
                commits={commits}
                buckets={commitBuckets}
              />
            </div>
          </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
