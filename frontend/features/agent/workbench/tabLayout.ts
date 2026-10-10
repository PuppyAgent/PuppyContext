// Ported from Desktop auxiliary-workbench/workbenchSessionHeaderLayout.ts at
// puppyone 2c113d5e. Keep the capacity rules aligned with the native workbench.
export const WORKBENCH_SESSION_HEADER_METRICS = Object.freeze({
  gap: 3,
  fullMaximum: 144,
  fullMinimum: 104,
  compact: 28,
  overflowControl: 28,
  createControl: 28,
  absoluteActiveMinimum: 64,
  layoutMotionMs: 200,
});

export type WorkbenchSessionHeaderLayoutMode = "full" | "compact" | "overflow";

export type WorkbenchSessionHeaderLayout = {
  mode: WorkbenchSessionHeaderLayoutMode;
  activeTabWidth: number;
  inactiveTabWidth: number;
  tabBounds: readonly WorkbenchSessionTabBounds[];
  tabsWidth: number;
  visibleSessionIds: readonly string[];
  hiddenSessionIds: readonly string[];
};

export type WorkbenchSessionTabBounds = {
  inlineStart: number;
  sessionId: string;
  width: number;
};

type ResolveWorkbenchSessionHeaderLayoutInput = {
  sessionIds: readonly string[];
  activeSessionId: string | null;
  availableWidth: number;
  preferredVisibleSessionIds?: readonly string[];
};

/**
 * Resolves density from the rail capacity supplied by the Header container.
 * The active session is always visible; overflow retains its nearest siblings
 * in stable session order.
 */
export function resolveWorkbenchSessionHeaderLayout({
  sessionIds,
  activeSessionId,
  availableWidth,
  preferredVisibleSessionIds = [],
}: ResolveWorkbenchSessionHeaderLayoutInput): WorkbenchSessionHeaderLayout {
  const metrics = WORKBENCH_SESSION_HEADER_METRICS;
  const count = sessionIds.length;
  if (count === 0) {
    return {
      mode: "full",
      activeTabWidth: metrics.fullMaximum,
      inactiveTabWidth: metrics.fullMaximum,
      tabBounds: [],
      tabsWidth: 0,
      visibleSessionIds: [],
      hiddenSessionIds: [],
    };
  }

  const effectiveActiveId = sessionIds.includes(activeSessionId ?? "")
    ? activeSessionId as string
    : sessionIds[0];
  const width = Math.max(0, Math.floor(availableWidth));
  if (width === 0) return allVisible("full", metrics.fullMaximum);

  const fullWidth = Math.floor((width - metrics.gap * (count - 1)) / count);
  if (fullWidth >= metrics.fullMinimum) {
    return allVisible("full", Math.min(metrics.fullMaximum, fullWidth));
  }

  const compactInactiveWidth = (count - 1) * metrics.compact;
  const compactGaps = metrics.gap * (count - 1);
  const compactActiveWidth = width - compactInactiveWidth - compactGaps;
  if (compactActiveWidth >= metrics.fullMinimum) {
    return createLayout(
      "compact",
      Math.min(metrics.fullMaximum, compactActiveWidth),
      metrics.compact,
      sessionIds,
      [],
    );
  }

  const availableForCompactTabs = Math.max(
    0,
    width - metrics.fullMinimum - metrics.overflowControl - metrics.gap,
  );
  const visibleInactiveCount = Math.min(
    count - 1,
    Math.floor(availableForCompactTabs / (metrics.compact + metrics.gap)),
  );
  const visibleCount = visibleInactiveCount + 1;
  const visibleSessionIds = canPreserveVisibleWindow({
    activeSessionId: effectiveActiveId,
    preferredVisibleSessionIds,
    sessionIds,
    visibleCount,
  })
    ? [...preferredVisibleSessionIds]
    : nearestSessionWindow(sessionIds, effectiveActiveId, visibleInactiveCount);
  const activeTabWidth = Math.max(
    metrics.absoluteActiveMinimum,
    Math.min(
      metrics.fullMaximum,
      width
        - metrics.overflowControl
        - metrics.gap
        - visibleInactiveCount * (metrics.compact + metrics.gap),
    ),
  );
  const visibleSet = new Set(visibleSessionIds);

  return createLayout(
    "overflow",
    activeTabWidth,
    metrics.compact,
    visibleSessionIds,
    sessionIds.filter((sessionId) => !visibleSet.has(sessionId)),
  );

  function allVisible(
    mode: WorkbenchSessionHeaderLayoutMode,
    tabWidth: number,
  ): WorkbenchSessionHeaderLayout {
    return createLayout(mode, tabWidth, tabWidth, sessionIds, []);
  }

  function createLayout(
    mode: WorkbenchSessionHeaderLayoutMode,
    activeTabWidth: number,
    inactiveTabWidth: number,
    visibleSessionIds: readonly string[],
    hiddenSessionIds: readonly string[],
  ): WorkbenchSessionHeaderLayout {
    let inlineStart = 0;
    const tabBounds = visibleSessionIds.map((sessionId) => {
      const width = sessionId === effectiveActiveId ? activeTabWidth : inactiveTabWidth;
      const bounds = { inlineStart, sessionId, width };
      inlineStart += width + metrics.gap;
      return bounds;
    });
    const tabsWidth = tabBounds.length === 0 ? 0 : inlineStart - metrics.gap;
    return {
      mode,
      activeTabWidth,
      inactiveTabWidth,
      tabBounds,
      tabsWidth,
      visibleSessionIds: [...visibleSessionIds],
      hiddenSessionIds: [...hiddenSessionIds],
    };
  }
}

/**
 * Preserve the rendered tab window while the user moves between tabs that are
 * already visible. Stable DOM identity lets ideal bounds interpolate so the
 * newly active tab pushes its siblings instead of replacing them.
 */
function canPreserveVisibleWindow({
  activeSessionId,
  preferredVisibleSessionIds,
  sessionIds,
  visibleCount,
}: {
  activeSessionId: string;
  preferredVisibleSessionIds: readonly string[];
  sessionIds: readonly string[];
  visibleCount: number;
}) {
  if (
    preferredVisibleSessionIds.length !== visibleCount
    || !preferredVisibleSessionIds.includes(activeSessionId)
  ) return false;

  const sessionIndexById = new Map(sessionIds.map((sessionId, index) => [sessionId, index]));
  let previousIndex = -1;
  for (const sessionId of preferredVisibleSessionIds) {
    const index = sessionIndexById.get(sessionId);
    if (index === undefined || index <= previousIndex) return false;
    previousIndex = index;
  }
  return true;
}

function nearestSessionWindow(
  sessionIds: readonly string[],
  activeSessionId: string,
  visibleInactiveCount: number,
) {
  const activeIndex = Math.max(0, sessionIds.indexOf(activeSessionId));
  const nearestIndexes = sessionIds
    .map((_sessionId, index) => index)
    .filter((index) => index !== activeIndex)
    .sort((left, right) => {
      const distance = Math.abs(left - activeIndex) - Math.abs(right - activeIndex);
      return distance || left - right;
    })
    .slice(0, visibleInactiveCount);

  return [activeIndex, ...nearestIndexes]
    .sort((left, right) => left - right)
    .map((index) => sessionIds[index]);
}
