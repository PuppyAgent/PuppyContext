import { useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  resolveWorkbenchSessionHeaderLayout,
  WORKBENCH_SESSION_HEADER_METRICS,
} from "./tabLayout";

/** Measures Header capacity, never the content-sized rail it controls. */
export function useWorkbenchSessionHeaderLayout(
  sessionIds: readonly string[],
  activeSessionId: string | null,
  trailingControlCount = 1,
) {
  const capacityRef = useRef<HTMLDivElement>(null);
  const [availableWidth, setAvailableWidth] = useState(0);
  const [motionReady, setMotionReady] = useState(false);
  const [visibleWindow, setVisibleWindow] = useState<readonly string[]>([]);

  useLayoutEffect(() => {
    const capacity = capacityRef.current;
    if (!capacity) return undefined;
    let measuredWidth = -1;
    let frame: number | null = null;
    let measurementFrame: number | null = null;
    const measure = () => {
      const headerWidth = Math.floor(capacity.getBoundingClientRect().width);
      if (headerWidth === measuredWidth) return;
      measuredWidth = headerWidth;
      setMotionReady(false);
      if (frame !== null) cancelAnimationFrame(frame);
      const nextWidth = Math.max(
        0,
        headerWidth
          - WORKBENCH_SESSION_HEADER_METRICS.createControl * trailingControlCount
          - WORKBENCH_SESSION_HEADER_METRICS.gap,
      );
      setAvailableWidth((current) => current === nextWidth ? current : nextWidth);
      frame = requestAnimationFrame(() => { frame = null; setMotionReady(headerWidth > 0); });
    };
    // ResizeObserver delivery must not synchronously resize its own subtree.
    const scheduleMeasure = () => {
      if (measurementFrame !== null) return;
      measurementFrame = requestAnimationFrame(() => { measurementFrame = null; measure(); });
    };
    measure();
    const observer = typeof ResizeObserver === "function"
      ? new ResizeObserver(scheduleMeasure)
      : null;
    observer?.observe(capacity);
    window.addEventListener("resize", scheduleMeasure);
    return () => {
      observer?.disconnect();
      if (frame !== null) cancelAnimationFrame(frame);
      if (measurementFrame !== null) cancelAnimationFrame(measurementFrame);
      window.removeEventListener("resize", scheduleMeasure);
    };
  }, [trailingControlCount]);

  const layout = useMemo(() => resolveWorkbenchSessionHeaderLayout({
    sessionIds,
    activeSessionId,
    availableWidth,
    preferredVisibleSessionIds: visibleWindow,
  }), [activeSessionId, availableWidth, sessionIds, visibleWindow]);

  useLayoutEffect(() => {
    setVisibleWindow((current) => arraysEqual(current, layout.visibleSessionIds)
      ? current
      : layout.visibleSessionIds);
  }, [layout.visibleSessionIds]);

  return { capacityRef, layout, motionReady };
}

function arraysEqual(left: readonly string[], right: readonly string[]) {
  return left.length === right.length
    && left.every((value, index) => value === right[index]);
}
