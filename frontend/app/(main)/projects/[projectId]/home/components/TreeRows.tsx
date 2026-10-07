import React from 'react';
import type { useWorkspaceRouter as useRouter } from '@/features/workspace/navigation';
import { T } from '../lib/tokens';
import type { TreeNode, DashboardEntrypoint } from '../lib/types';
import { FileIcon } from './FileIcon';

// Recursive file tree.  Two visual layers, kept in strict separation so
// they don't fight over the same pixels:
//
//   ─ Tree guide layer (always GREY, T.treeGuide):
//       Every elbow stub, every ╰─ hook, every parent-drawn delegate
//       line.  Pure structure — nesting + sibling continuation.
//
//   ─ Scope layer (cyan when in active AP scope):
//       Rendered as a single absolute-positioned band per row, anchored
//       to the SCOPE ROOT's content-column start (= `highlightAnchorDepth`).
//       This is the key trick: every highlighted row's cyan band shares
//       the same `left`, so vertically-adjacent highlighted rows visually
//       merge into one continuous rectangular block. An earlier version
//       set the bg per-row at the row's own depth, which created a
//       staircase along the band's left edge — visually ragged.
//
//   Rest-state passive tint: rows targeted by ANY access point (`hasAP`)
//   keep `T.rowAttached` (a near-imperceptible whisper) on the right
//   content column.  Hover paints `T.rowHover` on the right column too.
//   Both of these stay per-row because they're not scope-anchored
//   affordances — they belong to the row itself, not to a parent's scope.

// Per-row visual variants used by ConnectionsCanvas to render
// "context-only" rows alongside AP-attached rows:
//   ─ 'muted'       — a real file/folder kept around as a sibling
//                     example so the user can see what else lives at
//                     that level.  Renders dim + italic but stays
//                     clickable (it IS a real file).
//   ─ 'placeholder' — a synthetic "… N more" stand-in for siblings
//                     we chose not to render.  Renders dim + italic
//                     + non-navigable (no real path to route to).
// Default callers (the Data card) pass no variants → every row
// renders at full strength like before.
export type RowVariant = 'muted' | 'placeholder';

// Single source of truth for tree row height in CSS pixels.
// Exported so callers that need to position artifacts vertically
// against a row (e.g. ConnectionsCanvas centers an xyflow Handle
// at ROW_HEIGHT / 2 on each AP-attached row) can stay locked to
// this value instead of duplicating the magic number.  Drifted
// once already (handle was hardcoded to 18 from the old 36 row);
// the export makes that class of bug impossible going forward.
export const ROW_HEIGHT = 32;

export function TreeRows({
  nodes, depth, projectId, router, accessByPath,
  highlightedPaths, highlightAnchorDepth, highlightRootDepth,
  renderRowExtras, rowVariants,
}: {
  nodes: TreeNode[];
  depth: number;
  projectId: string;
  router: ReturnType<typeof useRouter>;
  accessByPath: Map<string, DashboardEntrypoint[]>;
  highlightedPaths: Set<string> | null;
  // Depth of the hovered AP's scope-root row.  -1 means "whole tree
  // scope" (root access surface) — band starts flush at row x=0.
  highlightAnchorDepth: number;
  // Depth at which a highlighted row should be rendered as the SCOPE
  // ROOT (using `T.rowHighlightRoot`, the brighter tint) rather than a
  // descendant.  Always ≥ 0:
  //   ─ scoped AP    → equals `highlightAnchorDepth` (the AP's own row)
  //   ─ whole-tree   → 0 (every root-level highlighted row is "a root")
  // Falsy / undefined falls back to `highlightAnchorDepth`-or-0 so older
  // callers keep working.
  highlightRootDepth?: number;
  // Optional per-row render slot — receives the row's full path,
  // returns extra DOM to inject INSIDE the row container (which is
  // `position: relative`, so absolute positioning works).  Used by
  // ConnectionsCanvas to attach an xyflow `<Handle>` per row so
  // edges can target a specific tree row by path.  Default no-op
  // keeps the Data card's TreeRows usage unchanged.
  renderRowExtras?: (path: string) => React.ReactNode;
  // Optional per-row variant map (path → variant).  When supplied,
  // rows whose path is in the map render in the corresponding muted
  // visual style — see RowVariant docs above.
  rowVariants?: Map<string, RowVariant>;
}) {
  // Single shared left edge for every highlighted row's cyan band.
  // Computed once per render outside the row loop because it's identical
  // for every row in this depth-recursion (and indeed every row across
  // the whole tree under one hovered AP).
  //
  // Three anchor cases:
  //   ─ whole-tree (-1):  band is flush left (no anchor row exists).
  //   ─ root-level (0):   anchor row has no elbow; sit the band 8px in
  //                       from the data box edge for breathing room.
  //   ─ nested  (≥1):     anchor row's elbow stub sits at row x =
  //                       16 + (depth-1)*20 + 10. We park the band 8px
  //                       to the RIGHT of that stub — close enough to
  //                       feel anchored to the elbow, far enough to
  //                       leave a visible gap between the stub and the
  //                       cyan accent stripe so they don't visually
  //                       fuse into one chunky bar.  The 8px is
  //                       narrower than the icon's leading margin, so
  //                       the band's accent stripe still lands left of
  //                       the icon's bounding box rather than slicing
  //                       into it.
  const highlightLeft =
    highlightAnchorDepth < 0
      ? 0
      : highlightAnchorDepth === 0
        ? 8
        : 16 + (highlightAnchorDepth - 1) * 20 + 10 + 8;

  // Resolve the scope-root depth used to pick the brighter tint.
  // See prop docs above for the fallback rationale.
  const rootDepth = highlightRootDepth ?? (highlightAnchorDepth >= 0 ? highlightAnchorDepth : 0);

  return (
    <>
      {nodes.map((node, idx) => {
        const { entry, children } = node;
        const isFolder = entry.type === 'folder';
        const isLast = idx === nodes.length - 1;
        const attachedAccess = accessByPath.get(entry.path) || [];
        const encodedPath = entry.path.split('/').map(s => encodeURIComponent(s)).join('/');

        const variant = rowVariants?.get(entry.path);
        const isMuted = variant === 'muted';
        const isPlaceholder = variant === 'placeholder';

        const hasAP = attachedAccess.length > 0;
        const isHighlighted = highlightedPaths?.has(entry.path) ?? false;
        // The "root" of the hovered AP's scope: same depth as `rootDepth`
        // AND in the highlight set.  Painted with `rowHighlightRoot` (the
        // brighter cyan) so the user can pick out which row the AP is
        // pinned to when its scope spans many descendants.
        const isHighlightRoot = isHighlighted && depth === rootDepth;
        // Right-column rest bg: passive `attached` whisper, or transparent.
        // The cyan scope highlight does NOT live here — it's painted by the
        // absolute layer below so the band's left edge can be anchored.
        const rightColRestBg = hasAP ? T.rowAttached : 'transparent';

        return (
          <React.Fragment key={entry.path}>
            <div
              data-row-path={entry.path}
              className={`flex items-center ${isPlaceholder ? '' : 'cursor-pointer'}`}
              style={{
                height: ROW_HEIGHT,
                position: 'relative',
              }}
              onClick={
                // Placeholder rows are synthetic ("… N more") — no real
                // path to route to.  Muted siblings ARE real files,
                // they just render dim, so click stays active.
                isPlaceholder
                  ? undefined
                  : () =>
                      router.push(
                        `/projects/${projectId}/data/${encodedPath}${entry.type ? `?type=${encodeURIComponent(entry.type)}` : ''}`,
                      )
              }
            >
              {/* Scope-highlight band — absolute, anchored to the scope
                  root's content-col start.  Shared `left` across every
                  highlighted row makes the band read as one continuous
                  rectangle (no per-row staircase).  z=0: sits behind
                  both indent and content columns.

                  No left accent stripe: an earlier version painted an
                  inset 2px cyan border, but full-saturation cyan against
                  the 6%-cyan fill read as too loud — the eye snapped to
                  the stripe before reading the row content.  The bg
                  fill alone gives a soft but unambiguous boundary, which
                  is plenty given that the highlighted rows are also
                  visually distinguished by name color lifted to text1. */}
              {isHighlighted && (
                <div
                  aria-hidden
                  style={{
                    position: 'absolute',
                    left: highlightLeft, top: 0, bottom: 0, right: 0,
                    background: isHighlightRoot ? T.rowHighlightRoot : T.rowHighlight,
                    pointerEvents: 'none',
                    zIndex: 0,
                    transition: `background-color 200ms ${T.ease}`,
                  }}
                />
              )}

              {/* Indent / elbow column — transparent, neutral grey guides.
                  Sits above the highlight band via z=1 so the elbow
                  stays visible if depth > anchorDepth (i.e., when this
                  row is below the scope-root's left edge). */}
              <div style={{
                width: 16 + depth * 20, flexShrink: 0, height: '100%',
                position: 'relative', zIndex: 1,
                display: 'flex',
                alignItems: 'center', justifyContent: 'flex-end',
              }}>
                {depth > 0 && (
                  // <rect> not <line>: SVG `<line stroke-width="1">` paints
                  // a CENTER stroke that straddles two pixels (here x ∈
                  // [25.5, 26.5] in row coords). Adjacent absolute <div>
                  // delegate-lines below use INTEGER pixel boundaries
                  // (`left: 26, width: 1` → x ∈ [26, 27]). Center-stroke
                  // vs integer-edge means the two lines sit a half-pixel
                  // apart and read as a noticeable misalignment on retina.
                  // <rect> respects integer pixel coords, lining up
                  // perfectly with the absolute delegate-line below.
                  <svg
                    width={20} height={ROW_HEIGHT}
                    style={{ position: 'absolute', right: 0, top: 0 }}
                    viewBox={`0 0 20 ${ROW_HEIGHT}`}
                    shapeRendering="crispEdges"
                  >
                    <rect
                      x={10} y={0} width={1}
                      height={isLast ? ROW_HEIGHT / 2 : ROW_HEIGHT}
                      fill={T.treeGuide}
                    />
                    <rect
                      x={10} y={ROW_HEIGHT / 2} width={10} height={1}
                      fill={T.treeGuide}
                    />
                  </svg>
                )}
              </div>

              {/* Content column — icon + name + count + spacer.  Carries
                  the row's own bg (rest-attached whisper or hover wash);
                  the cyan scope highlight is the absolute layer above.
                  z=1 keeps content above the scope band. */}
              <div
                className="group/row flex items-center"
                style={{
                  flex: 1, height: '100%',
                  paddingRight: 4,
                  position: 'relative', zIndex: 1,
                  background: rightColRestBg,
                  transition: `background-color 200ms ${T.ease}`,
                }}
                onMouseEnter={e => { e.currentTarget.style.background = T.rowHover; }}
                onMouseLeave={e => { e.currentTarget.style.background = rightColRestBg; }}
              >
                {/* Icon */}
                <div style={{
                  width: 20, height: 20, display: 'flex',
                  alignItems: 'center', justifyContent: 'center',
                  flexShrink: 0, marginRight: 8,
                }}>
                  <FileIcon type={entry.type} />
                </div>

                {/* Name. Highlighted rows lift to text1 regardless of file
                    vs folder — when an AP scope is on, the whole subtree
                    should read as "fully present", not muted.

                    Muted / placeholder rows render in italic + text3 to
                    signal "context only, not the focus" — these are the
                    sibling examples (and "… N more" stand-in) that
                    ConnectionsCanvas inserts so the user can read each
                    AP's surrounding social context without us cluttering
                    the canvas with every irrelevant file. */}
                <span
                  style={{
                    fontSize: 13, fontFamily: T.fontSans,
                    color: isMuted || isPlaceholder
                      ? T.text3
                      : isHighlighted
                        ? T.text1
                        : (isFolder ? T.text1 : T.text2),
                    fontWeight: isMuted || isPlaceholder ? 400 : (isFolder ? 500 : 400),
                    fontStyle: isMuted || isPlaceholder ? 'italic' : 'normal',
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    transition: `color 200ms ${T.ease}`,
                  }}
                  className={isPlaceholder ? '' : 'group-hover/row:!text-[var(--po-text)]'}
                >
                  {entry.name}
                </span>

                {/* Folder item count — tabular for vertical alignment */}
                {isFolder && entry.children_count != null && entry.children_count > 0 && (
                  <span style={{
                    fontSize: 11, marginLeft: 8, flexShrink: 0,
                    color: T.text3, fontVariantNumeric: 'tabular-nums',
                  }}>
                    {entry.children_count}
                  </span>
                )}

                {/* Spacer — the SVG overlay's vault endpoint dot lands in
                    this empty area to the right of the name. */}
                <div style={{ flex: 1 }} />
              </div>

              {renderRowExtras?.(entry.path)}
            </div>

            {/* Children — the absolute 1px column is a STRUCTURAL guide
                threading the parent's next sibling through the children
                block.  Always neutral grey: it's part of the structure
                layer, never the scope layer. */}
            {children.length > 0 && (
              <div style={{ position: 'relative' }}>
                {depth > 0 && !isLast && (
                  <div style={{
                    position: 'absolute',
                    left: 16 + (depth - 1) * 20 + 10,
                    top: 0, bottom: 0, width: 1,
                    background: T.treeGuide,
                  }} />
                )}
                <TreeRows
                  nodes={children} depth={depth + 1}
                  projectId={projectId} router={router}
                  accessByPath={accessByPath}
                  highlightedPaths={highlightedPaths}
                  highlightAnchorDepth={highlightAnchorDepth}
                  highlightRootDepth={rootDepth}
                  renderRowExtras={renderRowExtras}
                  rowVariants={rowVariants}
                />
              </div>
            )}
          </React.Fragment>
        );
      })}
    </>
  );
}
