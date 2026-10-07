'use client';

import React, { useState, useCallback, useRef } from 'react';
import { PendingTaskRenderer, usePendingNullValue } from './EtlStatusRenderer';

// ============================================
// Types
// ============================================
type JsonValue = string | number | boolean | null | JsonObject | JsonArray;
interface JsonObject {
  [key: string]: JsonValue;
}
type JsonArray = JsonValue[];

export interface ValueRendererProps {
  value: JsonValue;
  path?: string; // 当前节点的路径（用于打开文档编辑器）
  nodeKey?: string; // 当前节点的 key（用于检测 pending task）
  tableId?: string; // 当前 table 的 ID（用于精确匹配 pending task）
  isExpanded: boolean;
  isExpandable: boolean;
  isSelectingAccessPoint?: boolean;
  // 行内快速添加（用于 List/Dict 容器节点）
  showQuickAdd?: boolean;
  onQuickAdd?: () => void;
  onChange: (newValue: JsonValue) => void;
  onToggle: () => void;
  onSelect: () => void;
  onOpenDocument?: (path: string, value: string) => void; // 打开长文本编辑器
}

// ============================================
// Utils
// ============================================
function getTypeInfo(value: JsonValue): { type: string; color: string } {
  if (value === null) return { type: 'null', color: 'var(--po-json-null)' };
  if (typeof value === 'string') return { type: 'string', color: 'var(--po-text)' };
  if (typeof value === 'number') return { type: 'number', color: 'var(--po-json-number)' };
  if (typeof value === 'boolean') return { type: 'boolean', color: 'var(--po-json-boolean)' };
  if (Array.isArray(value)) return { type: 'array', color: 'var(--po-warning)' };
  if (typeof value === 'object') return { type: 'object', color: 'var(--po-success)' };
  return { type: 'unknown', color: 'var(--po-text-muted)' };
}

// ============================================
// Constants
// ============================================
const COLLAPSE_THRESHOLD = 50; // 超过这个长度或包含换行符时，默认折叠为单行胶囊

// ============================================
// Sub-components
// ============================================

// 基础类型值渲染器
function PrimitiveValueEditor({
  value,
  path,
  nodeKey,
  tableId,
  isSelectingAccessPoint,
  onChange,
  onSelect,
  onOpenDocument,
}: {
  value: JsonValue;
  path?: string;
  nodeKey?: string;
  tableId?: string;
  isSelectingAccessPoint?: boolean;
  onChange: (newValue: JsonValue) => void;
  onSelect: () => void;
  onOpenDocument?: (path: string, value: string) => void;
}) {
  const editableRef = useRef<HTMLDivElement>(null);
  const typeInfo = getTypeInfo(value);

  const pendingTask = usePendingNullValue(value, nodeKey, tableId);

  // 处理 contentEditable 保存
  const handleContentEditableBlur = useCallback(() => {
    if (editableRef.current) {
      const newValue = editableRef.current.innerText;
      if (newValue !== String(value)) {
        let parsedValue: JsonValue = newValue;
        if (newValue === 'true') parsedValue = true;
        else if (newValue === 'false') parsedValue = false;
        else if (newValue === 'null') parsedValue = null;
        else if (!isNaN(Number(newValue)) && newValue.trim() !== '')
          parsedValue = Number(newValue);
        onChange(parsedValue);
      }
    }
  }, [value, onChange]);

  const handleContentEditableKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (editableRef.current) {
          editableRef.current.innerText = String(value);
        }
        editableRef.current?.blur();
        return;
      }

      if (e.key === 'Enter') {
        if (typeof value === 'string') {
          if (e.metaKey || e.ctrlKey) {
            e.preventDefault();
            editableRef.current?.blur();
          }
        } else {
          e.preventDefault();
          editableRef.current?.blur();
        }
      }
    },
    [value]
  );

  const handleEditClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    onSelect();
  };

  // null 值 + pending task 检测
  // 如果值为 null 且对应一个正在处理的 ETL 任务，显示处理中状态
  if (value === null && nodeKey) {
    if (pendingTask) {
      return <PendingTaskRenderer />;
    }
  }

  // 字符串类型 - 简化版：短文本可编辑，长文本只显示字数
  if (typeof value === 'string') {
    const str = value;
    const hasNewline = str.includes('\n');
    const isLong = str.length > COLLAPSE_THRESHOLD || hasNewline;

    // 长文本：显示 preview + 字符数，点击打开文档编辑器
    if (isLong) {
      const handleOpenDoc = (e: React.MouseEvent) => {
        e.stopPropagation();
        if (onOpenDocument && path) {
          onOpenDocument(path, str);
        } else {
          onSelect();
        }
      };

      // 生成 preview：只加载前 300 个字符到 DOM，由 CSS 负责截断显示
      // 这样无论原字符串多长，DOM 中最多只有约 300 个字符
      const preview = str
        .slice(0, 300)
        .replace(/\n+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();

      return (
        <div
          onClick={handleOpenDoc}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            padding: '0 4px',
            margin: '0 -4px',
            borderRadius: 4,
            cursor: 'pointer',
            background: 'transparent',
            border: '1px solid transparent',
            width: '100%',
            overflow: 'hidden', // 关键：容器隐藏溢出
            height: 32,
            userSelect: 'none',
            transition: 'background 0.1s',
          }}
          onMouseEnter={e =>
            (e.currentTarget.style.background = 'var(--po-border-subtle)')
          }
          onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
        >
          {/* 图标 - 固定不收缩 */}
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              color: 'var(--po-text-subtle)',
              flexShrink: 0,
            }}
          >
            <svg
              width='14'
              height='14'
              viewBox='0 0 24 24'
              fill='none'
              stroke='currentColor'
              strokeWidth='2'
            >
              <path d='M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z'></path>
              <polyline points='14 2 14 8 20 8'></polyline>
              <line x1='16' y1='13' x2='8' y2='13'></line>
              <line x1='16' y1='17' x2='8' y2='17'></line>
              <polyline points='10 9 9 9 8 9'></polyline>
            </svg>
          </div>
          {/* Preview 文本 - 自适应宽度，CSS 自动截断 */}
          <span
            style={{
              fontSize: 14,
              color: 'var(--po-text)',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis', // 关键：超出显示 ...
              flex: 1, // 关键：占据剩余空间
              minWidth: 0, // 关键：允许收缩到 0，让 text-overflow 生效
              lineHeight: '28px',
            }}
          >
            {preview}
          </span>
          {/* 字符数 - 固定不收缩，始终完整显示 */}
          <span
            style={{
              fontSize: 12,
              color: 'var(--po-text-subtle)',
              whiteSpace: 'nowrap',
              flexShrink: 0, // 关键：禁止收缩
              lineHeight: '28px',
              paddingLeft: 8,
            }}
          >
            {str.length.toLocaleString()} chars
          </span>
        </div>
      );
    }

    // 短文本：可原地编辑
    return (
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '0 4px',
          margin: '0 -4px',
          width: '100%',
          overflow: 'hidden',
          height: 32,
        }}
      >
        {/* 图标 */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            color: 'var(--po-text-subtle)',
            flexShrink: 0,
          }}
        >
          <svg
            width='14'
            height='14'
            viewBox='0 0 24 24'
            fill='none'
            stroke='currentColor'
            strokeWidth='2'
          >
            <path d='M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z'></path>
            <polyline points='14 2 14 8 20 8'></polyline>
            <line x1='16' y1='13' x2='8' y2='13'></line>
            <line x1='16' y1='17' x2='8' y2='17'></line>
            <polyline points='10 9 9 9 8 9'></polyline>
          </svg>
        </div>
        {/* 可编辑内容 */}
        <div
          ref={editableRef}
          contentEditable={!isSelectingAccessPoint}
          suppressContentEditableWarning
          onBlur={handleContentEditableBlur}
          onKeyDown={handleContentEditableKeyDown}
          onClick={isSelectingAccessPoint ? undefined : handleEditClick}
          style={{
            color: typeInfo.color,
            fontSize: 14,
            flex: 1,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            lineHeight: '28px',
            borderRadius: 3,
            outline: 'none',
            cursor: isSelectingAccessPoint ? 'pointer' : 'text',
            transition: 'background 0.15s',
            pointerEvents: isSelectingAccessPoint ? 'none' : 'auto',
          }}
          onFocus={e => {
            e.currentTarget.style.background = 'var(--po-border-subtle)';
          }}
          onMouseEnter={e => {
            if (document.activeElement !== e.currentTarget) {
              e.currentTarget.style.background = 'var(--po-hover)';
            }
          }}
          onMouseLeave={e => {
            if (document.activeElement !== e.currentTarget) {
              e.currentTarget.style.background = 'transparent';
            }
          }}
        >
          {str}
        </div>
      </div>
    );
  }

  // 其他类型（number, boolean, null）
  return (
    <div
      ref={editableRef}
      contentEditable={!isSelectingAccessPoint}
      suppressContentEditableWarning
      onBlur={handleContentEditableBlur}
      onKeyDown={handleContentEditableKeyDown}
      onClick={isSelectingAccessPoint ? undefined : handleEditClick}
      style={{
        color: typeInfo.color,
        padding: '2px 4px',
        margin: '-2px -4px',
        borderRadius: 3,
        outline: 'none',
        cursor: isSelectingAccessPoint ? 'pointer' : 'text',
        transition: 'background 0.15s',
        pointerEvents: isSelectingAccessPoint ? 'none' : 'auto',
      }}
      onFocus={e => {
        e.currentTarget.style.background = 'var(--po-border-subtle)';
      }}
      onMouseEnter={e => {
        if (document.activeElement !== e.currentTarget) {
          e.currentTarget.style.background = 'var(--po-hover)';
        }
      }}
      onMouseLeave={e => {
        if (document.activeElement !== e.currentTarget) {
          e.currentTarget.style.background = 'transparent';
        }
      }}
    >
      {String(value)}
    </div>
  );
}

// 可展开类型切换器 (Object/Array)
function ExpandableToggle({
  value,
  isExpanded,
  isSelectingAccessPoint,
  showQuickAdd,
  onQuickAdd,
  onToggle,
}: {
  value: JsonValue;
  isExpanded: boolean;
  isSelectingAccessPoint?: boolean;
  showQuickAdd?: boolean;
  onQuickAdd?: () => void;
  onToggle: () => void;
}) {
  const [iconHovered, setIconHovered] = useState(false);
  const [addHovered, setAddHovered] = useState(false);

  const count = Array.isArray(value)
    ? value.length
    : Object.keys(value as object).length;
  const isArr = Array.isArray(value);
  const showAdd = !!showQuickAdd && !!onQuickAdd && !isSelectingAccessPoint;

  // 颜色调整：使用更低调的灰绿色/灰黄色
  const iconColor = isArr ? 'var(--po-warning)' : 'var(--po-success)'; // 降低亮度和饱和度 (amber-600 / emerald-600)

  return (
    <span
      onClick={
        isSelectingAccessPoint
          ? undefined
          : e => {
              e.stopPropagation();
              onToggle();
            }
      }
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        cursor: isSelectingAccessPoint ? 'pointer' : 'pointer',
        fontFamily: 'inherit',
        pointerEvents: isSelectingAccessPoint ? 'none' : 'auto',
        userSelect: 'none',
        padding: '2px 0',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          marginLeft: -6,
          position: 'relative',
        }}
        onMouseEnter={() => setIconHovered(true)}
        onMouseLeave={() => setIconHovered(false)}
      >
        {/* 图标尺寸 22px，保持清晰可辨识 */}
        <svg
          width='22'
          height='22'
          viewBox='0 0 22 22'
          fill='none'
          style={{ color: iconColor, opacity: 0.85 }}
        >
          {iconHovered ? (
            // Hover 状态：显示展开/收起箭头
            <g transform='scale(1.2222)'>
              {isExpanded ? (
                <path
                  d='M5 7L9 12L13 7'
                  stroke='currentColor'
                  strokeWidth='1.5'
                  strokeLinecap='round'
                  strokeLinejoin='round'
                />
              ) : (
                <path
                  d='M7 5L12 9L7 13'
                  stroke='currentColor'
                  strokeWidth='1.5'
                  strokeLinecap='round'
                  strokeLinejoin='round'
                />
              )}
            </g>
          ) : isArr ? (
            // Array (List) 图标：使用设计稿 Group 1144 (1)（[] + 3 cells，加粗版）
            // 原始 viewBox: 0 0 40 29 → 缩放进 22x22
            <g transform='translate(0 3.025) scale(0.55)'>
              <path
                d='M2.64205 26.7074V5.05114H8.80398V7.42045H5.60795V24.3466H8.80398V26.7074H2.64205ZM36.4964 5.05114V26.7074H30.3345V24.3466H33.5305V7.42045H30.3345V5.05114H36.4964Z'
                fill='currentColor'
              />
              <rect x='11' y='18' width='4' height='4' fill='currentColor' />
              <path d='M18 18H22V22H18V18Z' fill='currentColor' />
              <rect x='25' y='18' width='4' height='4' fill='currentColor' />
            </g>
          ) : (
            // Object (Dictionary) 图标：使用设计稿 Group 1142 (1)（{} + 3 cells，加粗版）
            // 原始 viewBox: 0 0 40 29 → 缩放进 22x22
            <g transform='translate(0 3.025) scale(0.55)'>
              <path
                d='M0.792614 15.9432V14.4517C1.75852 14.4517 2.43182 14.2557 2.8125 13.8636C3.19886 13.4659 3.39205 12.821 3.39205 11.929V9.75568C3.39205 8.79545 3.51136 8.00852 3.75 7.39489C3.99432 6.78125 4.34943 6.30398 4.81534 5.96307C5.28125 5.62216 5.84943 5.38636 6.51989 5.25568C7.19034 5.11932 7.9517 5.05114 8.80398 5.05114V7.42045C8.13352 7.42045 7.61932 7.5142 7.26136 7.7017C6.90341 7.88352 6.65625 8.16477 6.51989 8.54545C6.3892 8.92614 6.32386 9.41193 6.32386 10.0028V12.7983C6.32386 13.2358 6.25 13.6449 6.10227 14.0256C5.95455 14.4062 5.68466 14.7415 5.29261 15.0312C4.90057 15.3153 4.34091 15.5398 3.61364 15.7045C2.89205 15.8636 1.9517 15.9432 0.792614 15.9432ZM8.80398 26.7074C7.9517 26.7074 7.19034 26.6392 6.51989 26.5028C5.84943 26.3722 5.28125 26.1364 4.81534 25.7955C4.34943 25.4545 3.99432 24.9773 3.75 24.3636C3.51136 23.75 3.39205 22.9631 3.39205 22.0028V19.8381C3.39205 18.946 3.19886 18.304 2.8125 17.9119C2.43182 17.5142 1.75852 17.3153 0.792614 17.3153V15.8239C1.9517 15.8239 2.89205 15.9062 3.61364 16.071C4.34091 16.2301 4.90057 16.4545 5.29261 16.7443C5.68466 17.0284 5.95455 17.3608 6.10227 17.7415C6.25 18.1222 6.32386 18.5284 6.32386 18.9602V21.7557C6.32386 22.3466 6.3892 22.8324 6.51989 23.2131C6.65625 23.5938 6.90341 23.8778 7.26136 24.0653C7.61932 24.2528 8.13352 24.3466 8.80398 24.3466V26.7074ZM0.792614 17.3153V14.4517H3.49432V17.3153H0.792614ZM38.3459 15.8239V17.3153C37.38 17.3153 36.7038 17.5142 36.3175 17.9119C35.9368 18.304 35.7464 18.946 35.7464 19.8381V22.0028C35.7464 22.9631 35.6243 23.75 35.38 24.3636C35.1413 24.9773 34.7891 25.4545 34.3232 25.7955C33.8572 26.1364 33.2891 26.3722 32.6186 26.5028C31.9482 26.6392 31.1868 26.7074 30.3345 26.7074V24.3466C31.005 24.3466 31.5192 24.2528 31.8771 24.0653C32.2351 23.8778 32.4794 23.5938 32.6101 23.2131C32.7464 22.8324 32.8146 22.3466 32.8146 21.7557V18.9602C32.8146 18.5284 32.8885 18.1222 33.0362 17.7415C33.1839 17.3608 33.4538 17.0284 33.8459 16.7443C34.2379 16.4545 34.7947 16.2301 35.5163 16.071C36.2436 15.9062 37.1868 15.8239 38.3459 15.8239ZM30.3345 5.05114C31.1868 5.05114 31.9482 5.11932 32.6186 5.25568C33.2891 5.38636 33.8572 5.62216 34.3232 5.96307C34.7891 6.30398 35.1413 6.78125 35.38 7.39489C35.6243 8.00852 35.7464 8.79545 35.7464 9.75568V11.929C35.7464 12.821 35.9368 13.4659 36.3175 13.8636C36.7038 14.2557 37.38 14.4517 38.3459 14.4517V15.9432C37.1868 15.9432 36.2436 15.8636 35.5163 15.7045C34.7947 15.5398 34.2379 15.3153 33.8459 15.0312C33.4538 14.7415 33.1839 14.4062 33.0362 14.0256C32.8885 13.6449 32.8146 13.2358 32.8146 12.7983V10.0028C32.8146 9.41193 32.7464 8.92614 32.6101 8.54545C32.4794 8.16477 32.2351 7.88352 31.8771 7.7017C31.5192 7.5142 31.005 7.42045 30.3345 7.42045V5.05114ZM38.3459 14.4517V17.3153H35.6442V14.4517H38.3459Z'
                fill='currentColor'
              />
              <rect x='11' y='18' width='4' height='4' fill='currentColor' />
              <path d='M18 18H22V22H18V18Z' fill='currentColor' />
              <rect x='25' y='18' width='4' height='4' fill='currentColor' />
            </g>
          )}
        </svg>

        {/* 快速添加按钮：hover 行时显示，样式仿照左侧 handle */}
        {showAdd && (
          <button
            type='button'
            aria-label={isArr ? 'Add item' : 'Add property'}
            title={isArr ? 'Add Item' : 'Add Property'}
            onMouseEnter={() => setAddHovered(true)}
            onMouseLeave={() => setAddHovered(false)}
            onClick={e => {
              e.stopPropagation();
              e.preventDefault();
              onQuickAdd?.();
            }}
            style={{
              width: 30,
              height: 30,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              background: addHovered
                ? 'color-mix(in srgb, var(--po-text) 22%, transparent)'
                : 'var(--po-active)',
              border: 'none',
              borderRadius: 4,
              cursor: 'pointer',
              transition: 'opacity 0.12s, background 0.1s',
              color: 'var(--po-text-muted)',
              padding: 0,
            }}
          >
            <svg width='14' height='14' viewBox='0 0 14 14' fill='none'>
              <path
                d='M7 3v8M3 7h8'
                stroke='currentColor'
                strokeWidth='1.3'
                strokeLinecap='round'
              />
            </svg>
          </button>
        )}

        {/* 数字：只在收起态显示，紧贴图标右侧 */}
        {!isExpanded && (
          <span
            style={{
              fontSize: 10,
              fontWeight: 500,
              color: iconColor,
              fontFamily: "var(--po-font-sans)",
              marginLeft: 2,
              opacity: 0.7,
            }}
          >
            {count}
          </span>
        )}
      </div>
    </span>
  );
}

// ============================================
// Main Component
// ============================================
export function ValueRenderer({
  value,
  path,
  nodeKey,
  tableId,
  isExpanded,
  isExpandable,
  isSelectingAccessPoint,
  showQuickAdd,
  onQuickAdd,
  onChange,
  onToggle,
  onSelect,
  onOpenDocument,
}: ValueRendererProps) {
  if (isExpandable) {
    return (
      <ExpandableToggle
        value={value}
        isExpanded={isExpanded}
        isSelectingAccessPoint={isSelectingAccessPoint}
        showQuickAdd={showQuickAdd}
        onQuickAdd={onQuickAdd}
        onToggle={onToggle}
      />
    );
  }

  return (
    <PrimitiveValueEditor
      value={value}
      path={path}
      nodeKey={nodeKey}
      tableId={tableId}
      isSelectingAccessPoint={isSelectingAccessPoint}
      onChange={onChange}
      onSelect={onSelect}
      onOpenDocument={onOpenDocument}
    />
  );
}

export default ValueRenderer;
