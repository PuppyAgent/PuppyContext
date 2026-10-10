import { Copy, Check, ChevronDown, ChevronRight, Terminal } from 'lucide-react';
import { useState, memo, useCallback, CSSProperties } from 'react';
import { Dots } from '@/components/loading';
import MarkdownRenderer from './MarkdownRenderer';

import type { MessagePart } from './types';
export type { MessagePart } from './types';

interface Message {
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
}

export interface BotMessageProps {
  message: Message;
  parts?: MessagePart[];
  isStreaming?: boolean;
}

function truncateCommand(cmd: string, maxLen = 50): string {
  if (!cmd) return '';
  const simplified = cmd
    .replace(/\/workspace\/data\.json/g, 'data.json')
    .replace(/\/tmp\/temp\.json/g, 'temp.json')
    .replace(
      /> \/tmp\/temp\.json && mv \/tmp\/temp\.json \/workspace\/data\.json/g,
      '→ save'
    );
  if (simplified.length <= maxLen) return simplified;
  return simplified.substring(0, maxLen) + '...';
}

// ── Static styles (no re-allocation per render) ──

const S_TOOL_WRAP: CSSProperties = { marginBottom: 4 };

const S_TOOL_BTN_BASE: CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 6,
  height: 30,
  padding: '0 8px',
  fontSize: 11,
  backgroundColor: 'var(--po-hover)',
  borderRadius: 4,
  maxWidth: '100%',
  transition: 'background 0.15s',
};

const S_DOT_BASE: CSSProperties = {
  width: 10, height: 10, borderRadius: 2,
  display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
};

const S_CMD_SPAN: CSSProperties = {
  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
  fontFamily: 'var(--po-font-sans)', fontSize: 10, opacity: 0.7,
};

const S_DETAIL_BOX: CSSProperties = {
  marginTop: 4, marginLeft: 20, padding: '8px 10px',
  backgroundColor: 'var(--po-inset)', borderRadius: 4,
  fontSize: 10, fontFamily: 'var(--po-font-sans)',
  lineHeight: '1.5', maxHeight: 200, overflowY: 'auto',
};

const S_INPUT_TEXT: CSSProperties = {
  color: 'color-mix(in srgb, var(--po-text) 45%, transparent)', whiteSpace: 'pre-wrap', wordBreak: 'break-all',
};

const S_ICON_STYLE: CSSProperties = { color: 'var(--po-text-subtle)', flexShrink: 0 };

const S_MSG_OUTER: CSSProperties = {
  display: 'flex', flexDirection: 'column', width: '100%', gap: 8,
};

const S_COPY_BTN_BASE: CSSProperties = {
  display: 'flex', alignItems: 'center', justifyContent: 'center',
  width: 30, height: 30, borderRadius: 4, border: 'none',
  cursor: 'pointer', transition: 'all 0.15s',
};

// ── ToolItem ──

const ToolItem = memo(function ToolItem({
  part, isExpanded, onToggle,
}: { part: MessagePart; isExpanded: boolean; onToggle: () => void }) {
  const isRunning = part.toolStatus === 'running';
  const isCompleted = part.toolStatus === 'completed';
  const isError = part.toolStatus === 'error';

  const btnStyle: CSSProperties = {
    ...S_TOOL_BTN_BASE,
    color: isError ? 'var(--po-danger)' : 'var(--po-text-muted)',
    cursor: part.toolInput ? 'pointer' : 'default',
  };

  const dotStyle: CSSProperties = {
    ...S_DOT_BASE,
    border: `1.5px solid ${isError ? 'var(--po-danger)' : 'var(--po-border-strong)'}`,
  };

  return (
    <div style={S_TOOL_WRAP}>
      <div
        onClick={onToggle}
        style={btnStyle}
        onMouseEnter={e => { e.currentTarget.style.background = 'var(--po-border)'; }}
        onMouseLeave={e => { e.currentTarget.style.background = 'var(--po-hover)'; }}
      >
        {isRunning ? (
          <Dots size="xs" ariaLabel="Running tool" />
        ) : (
          <div style={dotStyle}>
            {isCompleted && <Check size={6} strokeWidth={3} style={{ color: 'var(--po-text-subtle)' }} />}
          </div>
        )}
        <Terminal size={11} style={S_ICON_STYLE} />
        <span style={S_CMD_SPAN}>{truncateCommand(part.toolInput || '', 60)}</span>
        {(part.toolInput || part.toolOutput) && (
          isExpanded
            ? <ChevronDown size={12} style={S_ICON_STYLE} />
            : <ChevronRight size={12} style={S_ICON_STYLE} />
        )}
      </div>

      {isExpanded && (part.toolInput || part.toolOutput) && (
        <div style={S_DETAIL_BOX}>
          {part.toolInput && (
            <div style={S_INPUT_TEXT}>
              <span style={{ color: 'var(--po-text-subtle)', marginRight: 6 }}>$</span>
              {part.toolInput}
            </div>
          )}
          {part.toolOutput && (
            <div style={{
              color: 'color-mix(in srgb, var(--po-text) 70%, transparent)',
              whiteSpace: 'pre-wrap', wordBreak: 'break-all',
              marginTop: part.toolInput ? 8 : 0,
              paddingTop: part.toolInput ? 8 : 0,
              borderTop: part.toolInput ? '1px solid var(--po-active)' : 'none',
            }}>
              {part.toolOutput}
            </div>
          )}
        </div>
      )}
    </div>
  );
});

// ── BotMessage ──

export default memo(function BotMessage({
  message, parts = [], isStreaming = false,
}: BotMessageProps) {
  const [isHovered, setIsHovered] = useState(false);
  const [copied, setCopied] = useState(false);
  const [expandedTools, setExpandedTools] = useState<Set<string>>(new Set());

  const handleCopy = useCallback(async () => {
    try {
      const copyPayload = message.content || '';
      if (!copyPayload) return;
      await navigator.clipboard.writeText(copyPayload);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {}
  }, [message.content]);

  const toggleTool = useCallback((toolId: string) => {
    setExpandedTools(prev => {
      const next = new Set(prev);
      if (next.has(toolId)) next.delete(toolId);
      else next.add(toolId);
      return next;
    });
  }, []);

  const hasParts = parts.length > 0;

  return (
    <div
      style={S_MSG_OUTER}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
    >
      {hasParts ? (
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          {parts.map((part, idx) => {
            if (part.type === 'tool' && part.toolId) {
              return (
                <ToolItem
                  key={`tool-${part.toolId}`}
                  part={part}
                  isExpanded={expandedTools.has(part.toolId)}
                  onToggle={() => toggleTool(part.toolId!)}
                />
              );
            }
            if (part.type === 'text' && part.content) {
              return (
                <div key={`text-${idx}`}>
                  <MarkdownRenderer content={part.content} />
                </div>
              );
            }
            return null;
          })}
        </div>
      ) : (
        message.content && <MarkdownRenderer content={message.content} />
      )}

      {/* Copy button */}
      {isHovered && !isStreaming && message.content && (
        <div style={{ display: 'flex', gap: 4, paddingTop: 2 }}>
          <button
            onClick={handleCopy}
            style={{
              ...S_COPY_BTN_BASE,
              background: copied ? 'color-mix(in srgb, var(--po-success) 10%, transparent)' : 'var(--po-hover)',
              color: copied ? 'var(--po-success)' : 'color-mix(in srgb, var(--po-text) 40%, transparent)',
            }}
            onMouseEnter={e => { e.currentTarget.style.background = 'var(--po-border)'; }}
            onMouseLeave={e => { e.currentTarget.style.background = 'var(--po-hover)'; }}
          >
            {copied ? <Check size={13} /> : <Copy size={13} />}
          </button>
        </div>
      )}
    </div>
  );
});
