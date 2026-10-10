'use client';

import { useMemo, useRef, useState, useEffect, type CSSProperties, type KeyboardEvent } from 'react';
import { ChevronDown, History, Plus, SquareDashed, X } from 'lucide-react';
import { AgentChatBrand } from '@/components/chat/AgentChatChrome';
import { runActive } from '../runtime/types';
import type { AgentWorkbenchController, WorkbenchTab, WorkbenchState } from './controller';
import { useWorkbenchSessionHeaderLayout } from './useTabLayout';
import styles from './workbench.module.css';

export const tabId = (id: string) => `cloud-agent-tab-${id}`;
export const panelId = (id: string) => `cloud-agent-panel-${id}`;
export function tabTitle(tab: WorkbenchTab, store: AgentWorkbenchController, state: WorkbenchState) {
  if (tab.kind === 'launcher') return tab.history ? 'Chat history' : 'New tab';
  const chat = store.actor(tab).getSnapshot();
  return state.sessions.find(session => session.id === chat.sessionId)?.title
    || chat.runs[0]?.prompt.slice(0, 80)
    || state.agents.find(agent => agent.id === tab.agentId)?.name || 'Agent';
}

export function AgentWorkbenchHeader({ store, state }: { store: AgentWorkbenchController; state: WorkbenchState }) {
  const ids = useMemo(() => state.tabs.map(tab => tab.id), [state.tabs]);
  const { capacityRef, layout } = useWorkbenchSessionHeaderLayout(ids, state.activeId);
  const [overflow, setOverflow] = useState(false);
  const header = useRef<HTMLElement>(null);
  const overflowButton = useRef<HTMLButtonElement>(null);
  const dragId = useRef<string | null>(null);
  useEffect(() => {
    if (!overflow) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !header.current?.contains(event.target)) setOverflow(false);
    };
    const escape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); setOverflow(false); overflowButton.current?.focus(); }
    };
    document.addEventListener('pointerdown', outside); document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape); };
  }, [overflow]);
  const activate = (id: string) => {
    store.activate(id); setOverflow(false);
    requestAnimationFrame(() => document.getElementById(tabId(id))?.focus({ preventScroll: true }));
  };
  const close = (id: string) => {
    if (!store.close(id)) return;
    const next = store.getSnapshot().activeId;
    requestAnimationFrame(() => next ? document.getElementById(tabId(next))?.focus() : header.current?.querySelector<HTMLButtonElement>('[aria-label="New tab"]')?.focus());
  };
  const keyDown = (event: KeyboardEvent<HTMLButtonElement>, id: string) => {
    const index = ids.indexOf(id);
    const step = document.documentElement.dir === 'rtl' ? -1 : 1;
    let next: number | null = null;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = ids.length - 1;
    if (event.key === 'ArrowRight') next = (index + step + ids.length) % ids.length;
    if (event.key === 'ArrowLeft') next = (index - step + ids.length) % ids.length;
    if (event.key === 'Delete') { event.preventDefault(); close(id); return; }
    if (next !== null) { event.preventDefault(); activate(ids[next]); }
  };
  const mark = (tab: WorkbenchTab) => {
    if (tab.kind === 'launcher') return tab.history ? <History size={14} /> : <SquareDashed size={14} />;
    const running = runActive(store.actor(tab).getSnapshot().runs.at(-1));
    return <span className={styles.agentMark} data-running={running || undefined}><AgentChatBrand />{running && <i />}</span>;
  };
  return <header ref={header} className={styles.header} data-sidebar-swipe-surface>
    <div ref={capacityRef} className={styles.capacity}>
      <div className={styles.rail} data-layout={layout.mode}>
        <div role='tablist' aria-label='Agent tabs' className={styles.tabs} style={{ width: layout.tabsWidth }}>
          {layout.tabBounds.map(bounds => {
            const tab = state.tabs.find(row => row.id === bounds.sessionId)!;
            const active = tab.id === state.activeId;
            const title = tabTitle(tab, store, state);
            return <div key={tab.id} className={styles.tab} data-active={active || undefined}
              data-compact={!active && layout.mode !== 'full' || undefined}
              style={{ insetInlineStart: bounds.inlineStart, width: bounds.width } as CSSProperties}
              onDragOver={event => { if (dragId.current) event.preventDefault(); }}
              onDrop={event => { event.preventDefault(); if (dragId.current) store.move(dragId.current, tab.id); dragId.current = null; }}>
              <button id={tabId(tab.id)} role='tab' type='button' className={styles.select}
                aria-controls={panelId(tab.id)} aria-selected={active} tabIndex={active ? 0 : -1}
                aria-label={title} title={title} onClick={() => store.activate(tab.id)}
                onKeyDown={event => keyDown(event, tab.id)} draggable
                onDragStart={event => { dragId.current = tab.id; event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', tab.id); }}
                onDragEnd={() => { dragId.current = null; }}>
                <span className={styles.mark}>{mark(tab)}</span><span className={styles.title}>{title}</span>
              </button>
              <button type='button' className={`${styles.iconButton} ${styles.close}`} aria-label={`Close ${title}`}
                title={`Close ${title}`} onClick={() => close(tab.id)}><X size={12} strokeWidth={1.8} /></button>
            </div>;
          })}
        </div>
        {layout.hiddenSessionIds.length > 0 && <button ref={overflowButton} type='button' className={`${styles.iconButton} ${styles.overflowTrigger}`}
          style={{ insetInlineStart: layout.tabsWidth + 3 }} aria-label='More tabs' aria-expanded={overflow} onClick={() => setOverflow(value => !value)}><ChevronDown size={14} /></button>}
        <button type='button' className={`${styles.iconButton} ${styles.newTab}`} aria-label='New tab' title='New tab'
          style={{ insetInlineStart: layout.tabsWidth + 3 + (layout.hiddenSessionIds.length ? 31 : 0) }}
          onClick={() => { setOverflow(false); const id = store.newTab(); requestAnimationFrame(() => document.getElementById(tabId(id))?.focus()); }}><Plus size={14} strokeWidth={1.9} /></button>
      </div>
    </div>
    {overflow && layout.hiddenSessionIds.length > 0 && <div className={styles.overflowMenu} role='dialog' aria-label='More tabs'>
      {layout.hiddenSessionIds.map(id => {
        const tab = state.tabs.find(row => row.id === id)!;
        const title = tabTitle(tab, store, state);
        return <div key={id} className={styles.overflowRow}><button type='button' className={styles.overflowSelect} onClick={() => activate(id)}>{mark(tab)}<span>{title}</span></button>
          <button type='button' className={styles.iconButton} aria-label={`Close ${title}`} onClick={() => close(id)}><X size={12} /></button></div>;
      })}
    </div>}
  </header>;
}
