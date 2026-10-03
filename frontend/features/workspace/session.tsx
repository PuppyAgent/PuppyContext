'use client';

import { createContext, useCallback, useContext, useState, type ReactNode, type SetStateAction } from 'react';
import { createStore } from 'zustand/vanilla';
import { useStore } from 'zustand';
import { parseWorkspaceLocation, useNavigationGuard } from './navigation';
import { ActiveFileProvider } from './activeFile';

// Transient UI only: server facts remain in SWR; drafts keep their save-session owner.
interface SessionValues {
  filesHref: string | null;
  historyScope: string;
  historyActor: string | null;
  historyCommit: string | null;
  accessTarget: string | null;
  historyExpanded: boolean;
  historySectionOpen: boolean;
}

export type PanelType =
  | 'none'
  | 'version_history'
  | 'sync_config'
  | 'sync_create'
  | 'access_list'
  | 'agent_chat'
  | 'workspace_chat'
  | 'mcp_config'
  | 'sandbox_config';

export interface PanelState {
  type: PanelType;
  nodeId?: string;
  accessEndpointId?: string;
  /** Exact binding selected from a Synchronize resource, not an Access ID. */
  synchronizeBindingId?: string;
  agentId?: string;
  mcpEndpointId?: string;
  sandboxEndpointId?: string;
  /** When opening sync_create from a scope's "AI Agent" default, set
   *  this to 'chat' so the create panel skips the type-picker and lands
   *  directly on the chat-agent form. */
  agentTypePreselect?: 'chat';
  /** Drill-down state for the access_list panel: when set, the panel
   *  renders the detail view of *this specific scope* rather than the
   *  one matched against the current file-tree folder. Set by the
   *  Overview's row-click handler and by the file-tree row's chain
   *  icon; cleared by file-tree navigation or by the back button. */
  selectedTargetKey?: string;
  /** Explicit access_list view selection.
   *  - `'overview'`  — render the all-scopes list, regardless of
   *                    whether the current folder happens to be a
   *                    scope. Set by the back button so the user has
   *                    a stable "management home" to return to.
   *  - `'detail'`    — render scope detail (paired with selectedTargetKey
   *                    when drilling in from a non-scope folder).
   *  - `'settings'`  — render the selected scope's dedicated settings
   *                    page. This is a sibling of detail, not an inline
   *                    expansion inside it.
   *  - `undefined`   — auto: detail when current folder is a scope,
   *                    overview otherwise.
   *
   *  File-tree navigation only resets implicit/drilled `'detail'` (so
   *  it can re-pick a matching folder scope) — `'overview'`, `'settings'`,
   *  are explicit user choices and stay sticky until the user explicitly
   *  navigates away (back button / close). Creation is modal-owned. */
  view?: 'overview' | 'detail' | 'settings' | 'create';
}

export interface ProjectSession extends SessionValues {
  panel: PanelState;
  panelNavigationGuard: (() => boolean) | null;
  openPanel: (panel: PanelState) => void;
  closePanel: () => void;
  togglePanel: (panel: PanelState) => void;
  setPanelNavigationGuard: (guard: (() => boolean) | null) => void;
  setValue: <K extends keyof SessionValues>(key: K, value: SetStateAction<SessionValues[K]>) => void;
}

const NONE: PanelState = { type: 'none' };

export const createProjectSession = () => createStore<ProjectSession>((set, get) => ({
  filesHref: null,
  historyScope: '',
  historyActor: null,
  historyCommit: null,
  accessTarget: null,
  historyExpanded: false,
  historySectionOpen: true,
  setValue: (key, value) => set(state => {
    const next = typeof value === 'function' ? value(state[key]) : value;
    if (Object.is(next, state[key])) return state;
    // Collapse only on an actual filter change, never on a route remount.
    return key === 'historyScope' || key === 'historyActor'
      ? { [key]: next, historyExpanded: false }
      : { [key]: next };
  }),
  panel: NONE,
  panelNavigationGuard: null,

  openPanel: panel => {
    if (get().panelNavigationGuard?.() === false) return;
    set({ panel, panelNavigationGuard: null });
  },

  closePanel: () => {
    if (get().panelNavigationGuard?.() === false) return;
    set({ panel: NONE, panelNavigationGuard: null });
  },

  setPanelNavigationGuard: panelNavigationGuard => set({ panelNavigationGuard }),

  togglePanel: panel => {
    if (get().panelNavigationGuard?.() === false) return;
    const cur = get().panel;
    // For access_list, treat any same-type click as a toggle-close —
    // the panel auto-syncs to the current folder, so reopening with a
    // different nodeId is the same surface conceptually. Without this,
    // navigating the file tree would leave the chip's "click again to
    // close" behaviour broken because cur.nodeId and panel.nodeId
    // mismatch.
    if (panel.type === 'access_list') {
      set({ panel: cur.type === 'access_list' ? NONE : panel, panelNavigationGuard: null });
      return;
    }
    const isSame =
      cur.type === panel.type &&
      cur.nodeId === panel.nodeId &&
      cur.accessEndpointId === panel.accessEndpointId &&
      cur.agentId === panel.agentId &&
      cur.mcpEndpointId === panel.mcpEndpointId &&
      cur.sandboxEndpointId === panel.sandboxEndpointId &&
      cur.selectedTargetKey === panel.selectedTargetKey &&
      cur.view === panel.view;
    set({ panel: isSame ? NONE : panel, panelNavigationGuard: null });
  },
}));

const SessionContext = createContext<ReturnType<typeof createProjectSession> | null>(null);

function SessionOwner({ children, projectId }: { children: ReactNode; projectId: string }) {
  const [store] = useState(createProjectSession);
  useNavigationGuard(target => {
    const next = parseWorkspaceLocation(target.split('?')[0]);
    // Project-level Access persists across Files/Git; only leaving the project
    // destroys that form. Its local panel controls retain their own guard.
    return store.getState().panelNavigationGuard && next?.projectId !== projectId
      ? 'You have unsaved Access settings. Leave this project?' : true;
  });
  return <SessionContext.Provider value={store}><ActiveFileProvider>{children}</ActiveFileProvider></SessionContext.Provider>;
}

export function ProjectSessionProvider({ projectId, children }: { projectId: string; children: ReactNode }) {
  return <SessionOwner key={projectId} projectId={projectId}>{children}</SessionOwner>;
}

export function useProjectSessionApi() {
  const store = useContext(SessionContext);
  if (!store) throw new Error('Project session requires ProjectSessionProvider');
  return store;
}

export function useProjectSession<T>(selector: (state: ProjectSession) => T): T {
  return useStore(useProjectSessionApi(), selector);
}

/** File inspectors do not own project-level Agent/Access visibility. Mapping
 * those states to the same empty value keeps file reads/editors independent. */
export function selectFilePanel(state: ProjectSession): PanelState {
  return state.panel.type === 'workspace_chat' || state.panel.type === 'access_list' || state.panel.type === 'none'
    ? NONE : state.panel;
}

export function useSessionValue<K extends keyof SessionValues>(key: K) {
  const value = useProjectSession(state => state[key]);
  const setValue = useProjectSession(state => state.setValue);
  const set = useCallback((next: SetStateAction<SessionValues[K]>) => setValue(key, next), [key, setValue]);
  return [value, set] as const;
}

export function useWorkspaceChat() {
  const open = useProjectSession(state => state.panel.type === 'workspace_chat');
  const openPanel = useProjectSession(state => state.openPanel);
  const closePanel = useProjectSession(state => state.closePanel);
  const setOpen = useCallback((next: SetStateAction<boolean>) => {
    const value = typeof next === 'function' ? next(open) : next;
    if (value) openPanel({ type: 'workspace_chat' });
    else if (open) closePanel();
  }, [open, openPanel, closePanel]);
  return [open, setOpen] as const;
}
