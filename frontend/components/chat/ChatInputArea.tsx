'use client';

import { useRef, useLayoutEffect, forwardRef, useImperativeHandle, useId } from 'react';
import { ArrowUp, LoaderCircle, Square } from 'lucide-react';
import styles from './AgentChatSurface.module.css';

// Access 选项类型
export interface AccessOption {
  id: string;
  label: string;
  type: 'bash' | 'tool'; // bash = shell_access, tool = MCP tools
  icon?: React.ReactNode;
  tableId?: string; // 所属 table 的 ID
  tableName?: string; // 所属 table 的名称
}

interface ChatInputAreaProps {
  inputValue: string;
  onInputChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
  onKeyDown: (e: React.KeyboardEvent) => void;
  onSend: () => void;
  isLoading: boolean;
  onStop?: () => void;
  stopping?: boolean;
  // @ 补全相关
  showMentionMenu: boolean;
  filteredMentionOptions: string[];
  mentionIndex: number;
  onMentionSelect: (key: string) => void;
  onMentionIndexChange: (index: number) => void;
  onBlur?: () => void;
  // 可选
  placeholder?: string;
  disabled?: boolean;
}

export interface ChatInputAreaRef {
  focus: () => void;
  setSelectionRange: (start: number, end: number) => void;
}

const ChatInputArea = forwardRef<ChatInputAreaRef, ChatInputAreaProps>(
  function ChatInputArea({ inputValue, onInputChange, onKeyDown, onSend, isLoading, onStop, stopping = false,
    showMentionMenu, filteredMentionOptions, mentionIndex, onMentionSelect,
    onMentionIndexChange, onBlur, placeholder = 'Ask about this project', disabled = false }, ref) {
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const highlightRef = useRef<HTMLDivElement>(null);
    const menuId = useId();
    const mentionOpen = showMentionMenu && filteredMentionOptions.length > 0;

    useImperativeHandle(ref, () => ({
      focus: () => textareaRef.current?.focus(),
      setSelectionRange: (start, end) => {
        textareaRef.current?.focus();
        textareaRef.current?.setSelectionRange(start, end);
      },
    }));

    useLayoutEffect(() => {
      const textarea = textareaRef.current;
      if (!textarea) return;
      const resize = () => {
        textarea.style.height = '45px';
        textarea.style.height = `${Math.max(45, Math.min(textarea.scrollHeight, 144))}px`;
        if (highlightRef.current) highlightRef.current.scrollTop = textarea.scrollTop;
      };
      resize();
      // Reflow long drafts when the user resizes the sidebar.
      let previousWidth = textarea.clientWidth;
      const observer = new ResizeObserver(() => {
        if (textarea.clientWidth === previousWidth) return;
        previousWidth = textarea.clientWidth;
        resize();
      });
      observer.observe(textarea);
      return () => observer.disconnect();
    }, [inputValue]);

    return (
      <div className={styles.dock}>
        <div className={styles.composer} data-disabled={disabled || undefined}>
          {mentionOpen && (
            <div className={`${styles.popover} ${styles.mentionMenu}`} id={menuId} role='listbox' aria-label='Select data path'>
              <div className={styles.menuHeading}>Select data path</div>
              {filteredMentionOptions.map((key, index) => (
                <button type='button' key={key} id={`${menuId}-${index}`} role='option'
                  aria-selected={index === mentionIndex}
                  aria-current={index === mentionIndex ? 'true' : undefined}
                  className={styles.menuItem}
                  onMouseDown={event => event.preventDefault()}
                  onClick={() => onMentionSelect(key)}
                  onMouseEnter={() => onMentionIndexChange(index)}>
                  <span>@{key}</span>
                </button>
              ))}
            </div>
          )}
          <div className={styles.inputRow}>
            <div ref={highlightRef} className={styles.highlight} aria-hidden='true'>
              {inputValue.split(/(@(?:\[\d+\]|[\w\u4e00-\u9fa5.\-_]+)+)/).map((part, index) => (
                <span key={index} className={part.startsWith('@') ? styles.mention : undefined}>{part}</span>
              ))}{'\n'}
            </div>
            <textarea ref={textareaRef} className={styles.input} value={inputValue}
              aria-label='Message Agent' aria-controls={mentionOpen ? menuId : undefined}
              aria-autocomplete='list' aria-activedescendant={mentionOpen ? `${menuId}-${mentionIndex}` : undefined}
              onChange={onInputChange} onKeyDown={onKeyDown} onBlur={onBlur}
              onScroll={event => { if (highlightRef.current) highlightRef.current.scrollTop = event.currentTarget.scrollTop; }}
              placeholder={placeholder} disabled={disabled || isLoading} rows={1} />
          </div>
          <div className={styles.toolbar}>
            <span />
            <ComposerAction inputValue={inputValue} disabled={disabled} isLoading={isLoading}
              onStop={onStop} stopping={stopping} onSend={onSend} />
          </div>
        </div>
      </div>
    );
  }
);

export default ChatInputArea;

function ComposerAction({ inputValue, disabled, isLoading, onStop, stopping, onSend }:
  Pick<ChatInputAreaProps, 'inputValue' | 'disabled' | 'isLoading' | 'onStop' | 'stopping' | 'onSend'>) {
  let label = 'Send message';
  let action = onSend;
  let blocked = disabled || !inputValue.trim() || isLoading;
  let icon = <ArrowUp size={17} strokeWidth={1.6} />;
  if (isLoading) { label = 'Agent is responding'; icon = <LoaderCircle size={15} className={styles.spin} />; }
  if (isLoading && onStop) {
    label = stopping ? 'Stopping Agent' : 'Stop Agent'; action = onStop; blocked = Boolean(stopping);
    if (!stopping) icon = <Square size={13} fill='currentColor' />;
  }
  return <button type='button' className={styles.send} onClick={action}
    aria-label={label} aria-busy={isLoading || undefined} disabled={blocked}>{icon}</button>;
}
