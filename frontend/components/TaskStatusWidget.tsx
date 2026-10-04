'use client';

import React, { useState, useEffect, useCallback, useMemo } from 'react';
import {
  isTerminalStatus,
  getStatusDisplayText,
  cancelETLTask,
  ETLTaskStatus,
} from '../lib/etlApi';
import { usePendingTasks, useTaskActions, useTaskProgress } from '@/features/tasks/TaskProvider';
import type { PendingTask, TaskType } from '@/features/tasks/model';
import {
  ACTIVITY_BG,
  ACTIVITY_BORDER,
  ACTIVITY_SHADOW,
  ACTIVITY_WIDTH,
  activityCardStyle,
  activityHeaderStyle,
  activityTitleStyle,
} from './activityStyles';
import { ActivityIconButton } from './ActivityIconButton';
import { Dots } from './loading';

interface TaskWithStatus extends PendingTask {
  displayStatus:
    | ETLTaskStatus['status']
    | 'uploading'
    | 'finalizing'
    | 'downloading'
    | 'extracting'
    | 'creating_nodes';
  /**
   * Best-known upload progress 0..100 for the ``uploading`` phase.
   * Backed by ``PendingTask.progress`` but copied here so the widget
   * can render even if the field is briefly absent during a state
   * transition.
   */
  displayProgress?: number;
}

interface TaskStatusWidgetProps {
  /** Use absolute positioning within parent instead of fixed */
  inline?: boolean;
}

/**
 * 任务状态浮窗
 *
 * 简洁交互：
 * - 收起：小圆圈，点击展开
 * - 展开：任务列表 + 收起按钮 + 清空按钮
 * - 不自己轮询，通过 SWR 订阅后台任务
 */
export function TaskStatusWidget({ inline = false }: TaskStatusWidgetProps) {
  const pendingTasks = usePendingTasks();
  const progress = useTaskProgress();
  const { clearAllTasks, removeTaskById } = useTaskActions();
  const [isExpanded, setIsExpanded] = useState(false);
  const tasks: TaskWithStatus[] = useMemo(() => [...pendingTasks]
    .sort((a, b) => b.timestamp - a.timestamp)
    .map(task => ({
      ...task,
      displayStatus: task.status || 'pending',
      displayProgress: progress[JSON.stringify([task.projectId, task.taskId])] ?? task.progress,
    })), [pendingTasks, progress]);

  // Only list/status changes expand the widget; progress preserves a collapse.
  useEffect(() => {
    if (pendingTasks.some(task => !isTaskTerminal(task.status || 'pending'))) setIsExpanded(true);
  }, [pendingTasks]);

  const handleClear = useCallback(() => {
    clearAllTasks();
    setIsExpanded(false);
  }, [clearAllTasks]);

  // Per-task removal. For processing FILE tasks we ALSO call the
  // backend cancel endpoint (the worker honors it via the `cancelled`
  // terminal state). For SaaS tasks the backend doesn't expose cancel,
  // so removal is purely local (the in-flight job will still finish
  // server-side, the user just hides it from this widget).
  const handleRemoveTask = useCallback(async (task: TaskWithStatus) => {
    const isProcessing = !isTaskTerminal(task.displayStatus);
    const isFileTask = !task.taskType || task.taskType === 'file';
    if (isProcessing && isFileTask && !task.taskId.startsWith('-') && !task.taskId.startsWith('placeholder-')) {
      try {
        await cancelETLTask(task.taskId, 'file');
      } catch (e) {
        // Cancel failure is non-fatal — we still remove from the
        // widget. The next status poll will re-add it if the task
        // is somehow still alive on the server.
        console.warn('[TaskStatusWidget] cancel failed:', e);
      }
    }
    removeTaskById(task.projectId, task.taskId);
  }, [removeTaskById]);

  const processingCount = tasks.filter(
    t => !isTaskTerminal(t.displayStatus)
  ).length;
  const allTerminal = tasks.length > 0 && processingCount === 0;
  const failedCount = tasks.filter(t => t.displayStatus === 'failed').length;

  // Completed uploads should feel like a receipt, not permanent chrome.
  // Keep failures visible for manual review, but retire all-success batches
  // after a short grace period.
  useEffect(() => {
    if (!allTerminal || failedCount > 0) return;
    const timer = window.setTimeout(handleClear, 8000);
    return () => window.clearTimeout(timer);
  }, [allTerminal, failedCount, handleClear]);

  // 无任务时不显示
  if (tasks.length === 0) {
    return null;
  }

  // Position styles based on inline prop
  const containerStyle: React.CSSProperties = inline
    ? {
        position: 'relative',
        fontFamily: 'var(--po-font-sans)',
      }
    : {
        position: 'fixed',
        bottom: 20,
        right: 20,
        zIndex: 9999,
        fontFamily: 'var(--po-font-sans)',
      };

  return (
    <div style={containerStyle}>
      {!isExpanded ? (
        /* Collapsed pill — shares the same surface tokens as the
           Getting Started checklist so the bottom-right activity stack
           reads as one unified language. */
        <button
          onClick={() => setIsExpanded(true)}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            width: ACTIVITY_WIDTH,
            minHeight: 38,
            padding: '8px 14px',
            background: ACTIVITY_BG,
            border: ACTIVITY_BORDER,
            borderRadius: 999,
            cursor: 'pointer',
            boxShadow: ACTIVITY_SHADOW,
            backdropFilter: 'blur(28px) saturate(160%)',
            WebkitBackdropFilter: 'blur(28px) saturate(160%)',
            position: 'relative',
            color: 'var(--po-text)',
          }}
        >
          {processingCount > 0 ? (
            <Dots size="sm" tone="info" ariaLabel="Processing" />
          ) : failedCount > 0 ? (
            <svg width='14' height='14' viewBox='0 0 16 16' fill='none'>
              <path
                d='M4 4L12 12M12 4L4 12'
                stroke='var(--po-danger)'
                strokeWidth='2'
                strokeLinecap='round'
              />
            </svg>
          ) : (
            <svg width='14' height='14' viewBox='0 0 16 16' fill='none'>
              <path
                d='M4 8L7 11L12 5'
                stroke='var(--po-success)'
                strokeWidth='2'
                strokeLinecap='round'
                strokeLinejoin='round'
              />
            </svg>
          )}

          <span style={{ ...activityTitleStyle, flex: 1, textAlign: 'left' }}>
            {processingCount > 0 ? `${processingCount} processing` : 'Done'}
          </span>
          <span style={{ color: 'var(--po-text-subtle)', fontSize: 11 }}>
            {tasks.length} {tasks.length === 1 ? 'item' : 'items'}
          </span>
        </button>
      ) : (
        /* 展开状态 */
        <div style={activityCardStyle}>
          {/* 头部 */}
          <div
            style={activityHeaderStyle}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
              {processingCount > 0 ? (
                <Dots size="xs" tone="info" ariaLabel="Processing" />
              ) : (
                <svg width='12' height='12' viewBox='0 0 14 14' fill='none'>
                  <path
                    d='M3 7L6 10L11 4'
                    stroke='var(--po-success)'
                    strokeWidth='2'
                    strokeLinecap='round'
                    strokeLinejoin='round'
                  />
                </svg>
              )}
              <span style={activityTitleStyle}>
                {processingCount > 0 ? `${processingCount} processing` : 'Done'}
              </span>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 2, flexShrink: 0 }}>
              {/* Collapse — always available */}
              <ActivityIconButton
                kind="collapse"
                title="Collapse"
                onClick={() => setIsExpanded(false)}
              />
              {/* Clear — only when every task has reached a terminal
                  state, so the user can never accidentally lose track
                  of an in-flight upload by clicking the wrong button. */}
              {allTerminal && (
                <ActivityIconButton
                  kind="close"
                  title="Clear all"
                  onClick={handleClear}
                />
              )}
            </div>
          </div>

          {/* Task list */}
          <div style={{ maxHeight: 220, overflowY: 'auto' }}>
            {tasks.map(task => (
              <TaskRow
                key={task.taskId}
                task={task}
                onRemove={() => handleRemoveTask(task)}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** 判断是否为终态 */
function isTaskTerminal(status: string): boolean {
  return (
    status === 'completed' || status === 'failed' || status === 'cancelled'
  );
}

// SaaS 类型图标 - 使用图片文件
const SaasIcons: Record<string, (props: { size?: number }) => JSX.Element> = {
  notion: ({ size = 12 }) => (
    <img src="/icons/notion.svg" alt="Notion" width={size} height={size} style={{ display: 'block' }} />
  ),
  github: ({ size = 12 }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
    </svg>
  ),
  airtable: ({ size = 12 }) => (
    <img src="/icons/airtable.png" alt="Airtable" width={size} height={size} style={{ display: 'block' }} />
  ),
  google_sheets: ({ size = 12 }) => (
    <img src="/icons/google_sheet.svg" alt="Google Sheets" width={size} height={size} style={{ display: 'block' }} />
  ),
  linear: ({ size = 12 }) => (
    <img src="/icons/linear.svg" alt="Linear" width={size} height={size} style={{ display: 'block' }} />
  ),
  gmail: ({ size = 12 }) => (
    <img src="/icons/gmail.svg" alt="Gmail" width={size} height={size} style={{ display: 'block' }} />
  ),
  drive: ({ size = 12 }) => (
    <img src="/icons/google_drive.svg" alt="Google Drive" width={size} height={size} style={{ display: 'block' }} />
  ),
  calendar: ({ size = 12 }) => (
    <img src="/icons/google_calendar.svg" alt="Google Calendar" width={size} height={size} style={{ display: 'block' }} />
  ),
};

// 获取 SaaS 状态文本
function getSaasStatusText(status: string): string {
  switch (status) {
    case 'pending': return 'Waiting...';
    case 'downloading': return 'Downloading...';
    case 'extracting': return 'Extracting...';
    case 'uploading': return 'Uploading...';
    case 'creating_nodes': return 'Creating...';
    case 'completed': return 'Done';
    case 'failed': return 'Failed';
    case 'cancelled': return 'Cancelled';
    default: return status;
  }
}

/** 单个任务行 */
function TaskRow({
  task,
  onRemove,
}: {
  task: TaskWithStatus;
  onRemove: () => void;
}) {
  const [hovered, setHovered] = useState(false);
  const isSaasTask = task.taskType && task.taskType !== 'file';
  const SaasIcon = isSaasTask && task.taskType ? SaasIcons[task.taskType] : null;

  const getStatusColor = () => {
    switch (task.displayStatus) {
      case 'completed':
        return 'var(--po-success)';
      case 'failed':
        return 'var(--po-danger)';
      case 'uploading':
        return 'var(--po-warning)';
      case 'finalizing':
        // Same palette family as ``processing`` — the user crossed
        // the "bytes uploaded" threshold so the row should advance
        // visually to the cool blue, signalling "the work is on
        // the server now, just waiting for it to land".
        return 'var(--po-accent)';
      default:
        return 'var(--po-accent)';
    }
  };

  const isProcessing = !isTaskTerminal(task.displayStatus);

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        padding: '9px 12px',
        borderBottom: '1px solid var(--po-hover)',
      }}
    >
      {/* 状态图标 */}
      <div style={{ flexShrink: 0, position: 'relative' }}>
        {task.displayStatus === 'completed' ? (
          <svg width='14' height='14' viewBox='0 0 14 14' fill='none'>
            <path
              d='M3 7L6 10L11 4'
              stroke='var(--po-success)'
              strokeWidth='1.5'
              strokeLinecap='round'
              strokeLinejoin='round'
            />
          </svg>
        ) : task.displayStatus === 'failed' ? (
          <svg width='14' height='14' viewBox='0 0 14 14' fill='none'>
            <path
              d='M3.5 3.5L10.5 10.5M10.5 3.5L3.5 10.5'
              stroke='var(--po-danger)'
              strokeWidth='1.5'
              strokeLinecap='round'
            />
          </svg>
        ) : (
          <Dots size="xs" tone={task.displayStatus === 'uploading' ? 'warning' : 'info'} ariaLabel="Processing" />
        )}

        {/* SaaS 图标角标 */}
        {SaasIcon && (
          <div style={{
            position: 'absolute',
            bottom: -3,
            right: -5,
            background: 'var(--po-panel-raised)',
            border: '1px solid var(--po-border-subtle)',
            borderRadius: 3,
            padding: 1,
            color: 'var(--po-text-muted)',
          }}>
            <SaasIcon size={8} />
          </div>
        )}
      </div>

      {/* Filename + status */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            color: 'var(--po-text)',
            fontSize: 13,
            lineHeight: '18px',
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {task.filename}
        </div>
        <div style={{ color: getStatusColor(), fontSize: 11, lineHeight: '16px', marginTop: 1 }}>
          {(() => {
            if (isSaasTask) return getSaasStatusText(task.displayStatus);
            if (
              task.displayStatus === 'uploading' &&
              typeof task.displayProgress === 'number'
            ) {
              return `Uploading ${task.displayProgress}%`;
            }
            if (task.displayStatus === 'finalizing') return 'Finalizing…';
            if (task.displayStatus === 'failed' && task.error) {
              return `Failed — ${task.error}`;
            }
            return getStatusDisplayText(
              task.displayStatus as ETLTaskStatus['status'] | 'uploading',
            );
          })()}
        </div>
        {/*
          Progress bar — drawn during ``uploading`` (real progress
          ticks 0..100 from XHR.upload.progress) and held at 100%
          during ``finalizing`` while the row's official loader signals
          that server-side work is still active.
        */}
        {(task.displayStatus === 'uploading' ||
          task.displayStatus === 'finalizing') &&
          typeof task.displayProgress === 'number' && (
            <div
              style={{
                marginTop: 4,
                width: '100%',
                height: 3,
                background: 'var(--po-border)',
                borderRadius: 2,
                overflow: 'hidden',
              }}
            >
              <div
                style={{
                  width:
                    task.displayStatus === 'finalizing'
                      ? '100%'
                      : `${task.displayProgress}%`,
                  height: '100%',
                  background:
                    task.displayStatus === 'finalizing'
                      ? 'var(--po-accent)'
                      : 'var(--po-warning)',
                  transition: 'width 0.2s ease',
                }}
              />
            </div>
          )}
      </div>

      {/* Per-row action — cancel for in-flight tasks, remove for done.
          Visible at low opacity always so the affordance is discoverable
          without being noisy; brightens on row hover. */}
      <button
        type="button"
        aria-label={isProcessing ? 'Cancel task' : 'Remove from list'}
        title={isProcessing ? 'Cancel' : 'Remove'}
        onClick={(e) => { e.stopPropagation(); onRemove(); }}
        style={{
          flexShrink: 0,
          width: 22,
          height: 22,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: 0,
          border: 'none',
          borderRadius: 5,
          background: 'transparent',
          cursor: 'pointer',
          color: hovered ? 'var(--po-text-muted)' : 'var(--po-text-disabled)',
          opacity: hovered ? 1 : 0.6,
          transition: 'color 0.12s ease, opacity 0.12s ease, background 0.12s ease',
        }}
        onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--po-border-subtle)'; }}
        onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}
      >
        {isProcessing ? (
          /* Stop square — communicates "halt the in-flight job". */
          <svg width="9" height="9" viewBox="0 0 10 10" fill="currentColor" aria-hidden>
            <rect x="1.5" y="1.5" width="7" height="7" rx="1" />
          </svg>
        ) : (
          /* Cross — communicates "remove this row from the list". */
          <svg width="11" height="11" viewBox="0 0 14 14" fill="none" aria-hidden>
            <path d="M3.5 3.5L10.5 10.5M10.5 3.5L3.5 10.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        )}
      </button>
    </div>
  );
}

export default TaskStatusWidget;
