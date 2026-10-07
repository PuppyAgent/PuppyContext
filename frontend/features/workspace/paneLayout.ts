/** Geometry contract for the Web workbench. No viewport/device breakpoints.
 * Preferences and open intent are inputs; this function never writes them. */
export type PanePresentation = 'docked' | 'overlay' | 'absent';
export type PaneConstraint = { present: boolean; preferredWidth: number; minWidth: number; maxWidth: number };
export type WorkspaceSegments = {
  orientation: 'horizontal' | 'vertical';
  first: number;
  second: number;
  gap: number;
};
export type WorkspaceLayoutInput = {
  availableWidth: number;
  mainMinWidth: number;
  projects: PaneConstraint & { collapsed: boolean; collapsedWidth: number };
  files: PaneConstraint;
  auxiliary: PaneConstraint & { open: boolean };
  inspector: PaneConstraint & { open: boolean };
  segments: WorkspaceSegments | null;
};
export type ResolvedPane = { presentation: PanePresentation; width: number; resizeMax: number };
export type WorkspaceLayout = {
  availableWidth: number;
  mainWidth: number;
  projects: ResolvedPane;
  files: ResolvedPane;
  auxiliary: ResolvedPane;
  inspector: ResolvedPane;
  overlayOwner: 'auxiliary' | 'inspector' | null;
  sharedHeader: boolean;
  segments: WorkspaceSegments | null;
};

export const DEFAULT_WORKSPACE_INPUT: WorkspaceLayoutInput = {
  availableWidth: 1280,
  mainMinWidth: 320,
  projects: { present: true, preferredWidth: 220, minWidth: 160, maxWidth: 360, collapsed: false, collapsedWidth: 56 },
  files: { present: false, preferredWidth: 220, minWidth: 220, maxWidth: 480 },
  auxiliary: { present: false, open: false, preferredWidth: 450, minWidth: 300, maxWidth: 800 },
  inspector: { present: false, open: false, preferredWidth: 450, minWidth: 300, maxWidth: 800 },
  segments: null,
};

const size = (value: number) => Number.isFinite(value) ? Math.max(0, value) : 0;
const clamp = (value: number, min: number, max: number) => Math.min(Math.max(size(value), min), Math.max(min, max));

type PaneWidths = Record<'projects' | 'files' | 'auxiliary' | 'inspector', number>;
type DockedPanes = Record<keyof PaneWidths, boolean>;
type LayoutGeometry = {
  width: number;
  mainMin: number;
  budget: number;
  segments: WorkspaceSegments | null;
};

function activeSegments(input: WorkspaceLayoutInput): WorkspaceSegments | null {
  if (!input.auxiliary.present || !input.auxiliary.open) return null;
  const segments = input.segments;
  if (segments && segments.first > 0 && segments.second > 0) return segments;
  return null;
}

function preferredPaneWidths(input: WorkspaceLayoutInput): PaneWidths {
  const { projects, files, auxiliary, inspector } = input;
  return {
    projects: projects.collapsed ? projects.collapsedWidth : clamp(projects.preferredWidth, projects.minWidth, projects.maxWidth),
    files: clamp(files.preferredWidth, files.minWidth, files.maxWidth),
    auxiliary: clamp(auxiliary.preferredWidth, auxiliary.minWidth, auxiliary.maxWidth),
    inspector: clamp(inspector.preferredWidth, inspector.minWidth, inspector.maxWidth),
  };
}

function reservedPaneWidths(
  input: WorkspaceLayoutInput,
  preferred: PaneWidths,
  docked: DockedPanes,
  segments: WorkspaceSegments | null,
): PaneWidths {
  return {
    projects: docked.projects ? preferred.projects : 0,
    files: docked.files ? input.files.minWidth : 0,
    auxiliary: docked.auxiliary && !segments ? input.auxiliary.minWidth : 0,
    inspector: docked.inspector ? input.inspector.minWidth : 0,
  };
}

function resolveDockedPanes(
  input: WorkspaceLayoutInput,
  preferred: PaneWidths,
  geometry: LayoutGeometry,
): DockedPanes {
  const { budget, mainMin, segments } = geometry;
  const docked = {
    projects: input.projects.present && !segments,
    files: input.files.present,
    auxiliary: input.auxiliary.present && input.auxiliary.open,
    inspector: input.inspector.present && input.inspector.open,
  };
  const minimum = () => {
    const reserved = reservedPaneWidths(input, preferred, docked, segments);
    return mainMin + reserved.projects + reserved.files + reserved.auxiliary + reserved.inspector;
  };

  // Presentations change only when all docked minima cannot fit. Projects
  // yield before Files; the document/Agent pair is retained as long as usable.
  if (minimum() > budget) docked.projects = false;
  if (minimum() > budget) docked.files = false;
  if (minimum() > budget) docked.inspector = false;
  if (minimum() > budget && !segments) docked.auxiliary = false;
  return docked;
}

function allocatePaneWidths(
  input: WorkspaceLayoutInput,
  preferred: PaneWidths,
  docked: DockedPanes,
  geometry: LayoutGeometry,
): PaneWidths {
  const { budget, mainMin, segments } = geometry;
  const widths = {
    projects: docked.projects ? preferred.projects : 0,
    files: docked.files ? preferred.files : 0,
    auxiliary: docked.auxiliary && !segments ? preferred.auxiliary : 0,
    inspector: docked.inspector ? preferred.inspector : 0,
  };
  let deficit = Math.max(0, mainMin - (budget - widths.projects - widths.files - widths.auxiliary - widths.inspector));
  // Reclaim preferred space in the same order, without changing stored intent.
  if (docked.auxiliary && !segments) {
    const reclaimed = Math.min(deficit, widths.auxiliary - input.auxiliary.minWidth);
    widths.auxiliary -= reclaimed;
    deficit -= reclaimed;
  }
  if (docked.inspector) {
    const reclaimed = Math.min(deficit, widths.inspector - input.inspector.minWidth);
    widths.inspector -= reclaimed;
    deficit -= reclaimed;
  }
  if (docked.files) widths.files -= Math.min(deficit, widths.files - input.files.minWidth);
  return widths;
}

function resolveNavigationPane(
  present: boolean,
  docked: boolean,
  width: number,
  overlayWidth: number,
  resizeMax: number,
): ResolvedPane {
  if (!present) return { presentation: 'absent', width: 0, resizeMax };
  if (docked) return { presentation: 'docked', width, resizeMax };
  return { presentation: 'overlay', width: overlayWidth, resizeMax };
}

function auxiliaryRenderedWidth(
  geometry: LayoutGeometry,
  overlay: boolean,
  open: boolean,
  allocated: number,
  preferred: number,
): number {
  const { segments, width } = geometry;
  if (segments?.orientation === 'horizontal') return segments.second;
  if (segments?.orientation === 'vertical') return width;
  if (overlay) return Math.max(0, Math.min(420, width - 32));
  // Closed regions retain a useful content width without reserving any space.
  return open ? allocated : preferred;
}

function inspectorRenderedWidth(
  open: boolean,
  docked: boolean,
  allocated: number,
  preferred: number,
  overlayWidth: number,
): number {
  if (!open) return preferred;
  return docked ? allocated : overlayWidth;
}

function resolveOverlayOwner(
  auxiliaryOpen: boolean,
  auxiliaryOverlay: boolean,
  inspectorOpen: boolean,
  inspectorDocked: boolean,
): WorkspaceLayout['overlayOwner'] {
  if (auxiliaryOpen && auxiliaryOverlay) return 'auxiliary';
  if (inspectorOpen && !inspectorDocked) return 'inspector';
  return null;
}

export function resolveWorkspaceLayout(input: WorkspaceLayoutInput): WorkspaceLayout {
  const width = size(input.availableWidth);
  const mainMin = size(input.mainMinWidth);
  const { projects, files, auxiliary, inspector } = input;
  const auxiliaryOpen = auxiliary.present && auxiliary.open;
  const inspectorOpen = inspector.present && inspector.open;
  const segments = activeSegments(input);
  const budget = segments?.orientation === 'horizontal' ? segments.first : width;
  const geometry = { width, mainMin, budget, segments };
  const preferred = preferredPaneWidths(input);
  const docked = resolveDockedPanes(input, preferred, geometry);
  const widths = allocatePaneWidths(input, preferred, docked, geometry);
  const reserved = reservedPaneWidths(input, preferred, docked, segments);
  const projectBodyWidth = Math.max(0, budget - widths.projects - widths.auxiliary - widths.inspector);
  const auxiliaryOverlay = !segments && (auxiliaryOpen ? !docked.auxiliary : width < mainMin + auxiliary.minWidth);

  return {
    availableWidth: width,
    mainWidth: Math.max(0, projectBodyWidth - widths.files),
    projects: resolveNavigationPane(
      projects.present,
      docked.projects,
      widths.projects,
      Math.max(0, Math.min(360, width - 40)),
      clamp(width - mainMin - reserved.files - reserved.auxiliary - reserved.inspector, projects.minWidth, projects.maxWidth),
    ),
    files: resolveNavigationPane(
      files.present,
      docked.files,
      widths.files,
      Math.max(0, Math.min(360, projectBodyWidth - 40)),
      clamp(budget - widths.projects - mainMin - reserved.auxiliary - reserved.inspector, files.minWidth, files.maxWidth),
    ),
    auxiliary: {
      presentation: auxiliaryOverlay ? 'overlay' : 'docked',
      width: auxiliaryRenderedWidth(geometry, auxiliaryOverlay, auxiliaryOpen, widths.auxiliary, preferred.auxiliary),
      resizeMax: clamp(width - widths.projects - widths.files - widths.inspector - mainMin, auxiliary.minWidth, auxiliary.maxWidth),
    },
    inspector: {
      presentation: inspectorOpen && !docked.inspector ? 'overlay' : 'docked',
      width: inspectorRenderedWidth(inspectorOpen, docked.inspector, widths.inspector, preferred.inspector,
        Math.max(0, budget - widths.projects - widths.auxiliary)),
      resizeMax: clamp(budget - widths.projects - widths.files - widths.auxiliary - mainMin, inspector.minWidth, inspector.maxWidth),
    },
    overlayOwner: resolveOverlayOwner(auxiliaryOpen, auxiliaryOverlay, inspectorOpen, docked.inspector),
    sharedHeader: !docked.projects || auxiliaryOverlay || Boolean(segments),
    segments,
  };
}
