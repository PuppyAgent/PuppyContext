'use client';

import { useMemo } from 'react';
import { TaskStatusWidget } from './TaskStatusWidget';
import { ImportJobsWidget } from './ImportJobsWidget';
import { UploadJobsWidget } from './UploadJobsWidget';
import { SyncJobsWidget } from './SyncJobsWidget';
import { useProjectActivity } from '@/lib/hooks/useActivity';

interface ActivityStackProps {
  projectId?: string;
}

/**
 * Single owner for bottom-right transient activity.
 *
 * Individual widgets should render inline inside this stack instead of
 * positioning themselves with `fixed`. This prevents independent overlays
 * (uploads, onboarding, future sync/export jobs) from competing for the
 * same screen corner.
 */
export function ActivityStack({
  projectId,
}: Readonly<ActivityStackProps>) {
  // One unified active-only request replaces three parallel reads of the same
  // activity view. The shell stays mounted across project sub-routes, so this
  // cache and polling loop also survive Files / Git / Access navigation.
  const { activeItems, refresh } = useProjectActivity(projectId, {
    activeOnly: true,
    limit: 30,
  });
  const itemsByKind = useMemo(() => ({
    upload: activeItems.filter(item => item.kind === 'upload'),
    import: activeItems.filter(item => item.kind === 'import'),
    sync: activeItems.filter(item => item.kind === 'synchronize_run'),
  }), [activeItems]);

  return (
    <div
      aria-label="Activity"
      style={{
        position: 'fixed',
        right: 12,
        bottom: 12,
        zIndex: 1000,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'flex-end',
        gap: 8,
        pointerEvents: 'none',
      }}
    >
      <div style={{ pointerEvents: 'auto' }}>
        <UploadJobsWidget activeItems={itemsByKind.upload} inline />
      </div>

      <div style={{ pointerEvents: 'auto' }}>
        <ImportJobsWidget
          activeItems={itemsByKind.import}
          onRefresh={refresh}
          inline
        />
      </div>

      <div style={{ pointerEvents: 'auto' }}>
        <SyncJobsWidget activeItems={itemsByKind.sync} inline />
      </div>

      <div style={{ pointerEvents: 'auto' }}>
        <TaskStatusWidget inline />
      </div>
    </div>
  );
}
