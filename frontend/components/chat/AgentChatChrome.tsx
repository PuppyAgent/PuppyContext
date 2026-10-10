'use client';

import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { ArrowLeft, Check, History, MoreHorizontal, Pencil, Plus, Settings2, X } from 'lucide-react';
import type { ChatSession } from './types';
import styles from './AgentChatSurface.module.css';

export function AgentChatBrand() {
  return <span className={styles.brand} aria-hidden='true'>
    <img className={styles.brandLight} src='/icons/agents/built-in-agent.svg' alt='' draggable={false} />
    <img className={styles.brandDark} src='/icons/agents/built-in-agent-dark.svg' alt='' draggable={false} />
  </span>;
}

export function AgentChatEmptyState({ onConfigure }: { onConfigure?: () => void }) {
  const [turns, setTurns] = useState(0);
  return (
    <div className={styles.empty} data-sidebar-swipe-surface>
      <button type='button' className={styles.emptyLogo} aria-label='Spin Agent logo'
        style={{ '--chat-logo-turns': turns } as CSSProperties} onClick={() => setTurns(value => value + 1)}>
        <AgentChatBrand />
      </button>
      <p>What should we work on?</p>
      {onConfigure && <button type='button' className={styles.setupLink} onClick={onConfigure}>Set up a chat agent</button>}
    </div>
  );
}

type AgentChatHeaderProps = {
  title: string;
  agentName?: string;
  sessions?: ChatSession[];
  currentSessionId?: string | null;
  busy?: boolean;
  onSelectSession?: (id: string) => void;
  onNewChat?: () => void;
  onClose?: () => void;
  onBack?: () => void;
  onSettings?: () => void;
  onRename?: (name: string) => Promise<void>;
};

export function AgentChatHeader({ title, agentName = 'Agent', sessions = [], currentSessionId,
  busy = false, onSelectSession, onNewChat, onClose, onBack, onSettings, onRename }: AgentChatHeaderProps) {
  const [menu, setMenu] = useState<'history' | 'actions' | 'rename' | null>(null);
  const [name, setName] = useState(agentName);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const headerRef = useRef<HTMLElement>(null);
  const historyRef = useRef<HTMLButtonElement>(null);
  const actionsRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!menu) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !headerRef.current?.contains(event.target)) setMenu(null);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      setMenu(null);
      (menu === 'history' ? historyRef : actionsRef).current?.focus();
    };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape, true);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', escape, true);
    };
  }, [menu]);

  return (
    <header className={styles.header} ref={headerRef} data-sidebar-swipe-surface>
      {onBack && <button type='button' className={styles.iconButton} aria-label='Back to integrations' onClick={onBack}><ArrowLeft size={14} /></button>}
      <div className={styles.tab}>
        <div className={styles.tabLabel} title={title}><AgentChatBrand /><span>{title}</span></div>
        {onClose && <button type='button' className={`${styles.iconButton} ${styles.close}`} aria-label='Close chat panel' title='Close chat panel' onClick={onClose}><X size={12} /></button>}
      </div>
      {onNewChat && <button type='button' className={styles.iconButton} aria-label='New chat' title='New chat' disabled={busy} onClick={() => { setMenu(null); onNewChat(); }}><Plus size={14} /></button>}
      <div className={styles.headerActions}>
        {onSelectSession && <button ref={historyRef} type='button' className={styles.iconButton} aria-label='Chat history' title='Chat history' aria-expanded={menu === 'history'} disabled={busy} onClick={() => setMenu(value => value === 'history' ? null : 'history')}><History size={14} /></button>}
        {(onSettings || onRename) && <button ref={actionsRef} type='button' className={styles.iconButton} aria-label='Chat actions' title='Chat actions' aria-expanded={menu === 'actions' || menu === 'rename'} onClick={() => setMenu(value => value ? null : 'actions')}><MoreHorizontal size={16} /></button>}
      </div>
      {menu === 'history' && <div className={styles.popover} aria-label='Chat history'>
        <div className={styles.menuHeading}>Chat history</div>
        {sessions.length ? sessions.map(session => <button type='button' key={session.id} className={styles.menuItem}
          aria-current={session.id === currentSessionId ? 'true' : undefined}
          onClick={() => { onSelectSession?.(session.id); setMenu(null); }}>
          <span>{session.title || 'New chat'}</span>
          <small>{new Date(session.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}</small>
        </button>) : <div className={styles.menuHeading}>No chat history yet</div>}
      </div>}
      {menu === 'actions' && <div className={styles.popover} aria-label='Chat actions'>
        {onRename && <button type='button' className={styles.menuItem} onClick={() => { setName(agentName); setError(''); setMenu('rename'); }}><Pencil size={14} />Rename agent</button>}
        {onSettings && <button type='button' className={styles.menuItem} onClick={() => { setMenu(null); onSettings(); }}><Settings2 size={14} />Agent settings</button>}
      </div>}
      {menu === 'rename' && <div className={styles.popover}>
        <form className={styles.renameForm} onSubmit={async event => {
          event.preventDefault();
          if (!name.trim() || !onRename || saving) return;
          setSaving(true);
          try { await onRename(name.trim()); setMenu(null); }
          catch { setError('Could not rename the agent. Try again.'); }
          finally { setSaving(false); }
        }}>
          <input aria-label='Agent name' autoFocus value={name} onChange={event => setName(event.target.value)} disabled={saving} />
          <button type='submit' className={styles.iconButton} aria-label='Save agent name' disabled={!name.trim() || saving}><Check size={14} /></button>
        </form>
        {error && <p role='alert' className={styles.menuError}>{error}</p>}
      </div>}
    </header>
  );
}
