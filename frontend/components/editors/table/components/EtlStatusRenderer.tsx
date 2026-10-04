'use client';

import React from 'react';
import { useParams } from 'next/navigation';
import { usePendingTasks } from '@/contexts/TaskProvider';
import { isTaskTerminal } from '@/lib/tasks/model';
import { PulseGrid } from '@/components/loading';

/** A same-named upload in another project/table cannot mark this cell pending. */
export function usePendingNullValue(value: unknown, filename?: string, tableId?: string) {
  const params = useParams<{ projectId?: string }>();
  const tasks = usePendingTasks(params?.projectId ?? null);
  if (value !== null || !filename) return undefined;
  return tasks.find(task => !isTaskTerminal(task.status) && task.filename === filename &&
    (!tableId || task.tableId === tableId));
}

export function PendingTaskRenderer() {
  return (
    <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <PulseGrid size='sm' tone='info' />
      <span style={{ fontSize: 13, color: 'var(--po-text-subtle)', fontStyle: 'italic' }}>
        Processing…
      </span>
    </div>
  );
}

export default PendingTaskRenderer;
