'use client';

import dynamic from 'next/dynamic';
import { useProjectSession } from '@/features/workspace/session';
import { PageLoading } from '@/components/loading';
import { useActiveFile } from '@/features/workspace/activeFile';

const AgentWorkbench = dynamic(
  () => import('../workbench/AgentWorkbench').then(module => module.AgentWorkbench),
  { ssr: false, loading: () => <PageLoading variant='fill' label='Loading chat' /> },
);

/** Project-owned chat tabs remain mounted when the auxiliary sidebar is hidden. */
export function ProjectChatPanel({ projectId, active }: {
  projectId: string; active: boolean; onClose: () => void;
}) {
  const activeFile = useActiveFile();
  const openPanel = useProjectSession(state => state.openPanel);
  const panel = useProjectSession(state => state.panel);
  return <AgentWorkbench projectId={projectId} active={active} tableData={activeFile.tableData}
    request={panel.type === 'workspace_chat' ? panel : undefined}
    onConfigure={() => openPanel({ type: 'access_list', view: 'overview' })} />;
}
