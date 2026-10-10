'use client';

import { useRef, useState } from 'react';
import dynamic from 'next/dynamic';
import { WorkspaceAuxiliaryRegion } from '@/components/sidebar/WorkspaceAuxiliaryRegion';
import { ProjectChatPanel } from '@/features/agent/components/ProjectChatPanel';
import { PageLoading } from '@/components/loading';
import { useProjectSession } from '@/features/workspace/session';

const ProjectAccessPanel = dynamic(
  () => import('./ProjectAccessPanel').then(module => ({ default: module.ProjectAccessPanel })),
  { ssr: false, loading: () => <PageLoading variant='fill' label='Loading access' /> },
);

export function ProjectAuxiliarySidebar({ projectId }: { projectId: string }) {
  const panelType = useProjectSession(state => state.panel.type);
  const closePanel = useProjectSession(state => state.closePanel);
  const chatOpen = panelType === 'workspace_chat';
  const accessOpen = panelType === 'access_list';
  const visible = chatOpen || accessOpen;
  const lastPanel = useRef(panelType);
  if (visible) lastPanel.current = panelType;
  const [chatMounted, setChatMounted] = useState(chatOpen);
  const [accessMounted, setAccessMounted] = useState(accessOpen);

  // Derive first-use retention before committing, so the region can focus its
  // content on the same opening commit. Subsequent closes only hide the panel.
  if (chatOpen && !chatMounted) setChatMounted(true);
  if (accessOpen && !accessMounted) setAccessMounted(true);

  return (
    <WorkspaceAuxiliaryRegion trigger={chatOpen || lastPanel.current === 'workspace_chat' ? 'chat' : 'access'} visible={visible} label={chatOpen ? 'Agent sidebar' : 'Access sidebar'} onClose={closePanel}>
      <div style={{ display: chatOpen || (!visible && lastPanel.current === 'workspace_chat') ? 'contents' : 'none' }}>
        {chatMounted && (
          <ProjectChatPanel projectId={projectId} active={chatOpen} onClose={closePanel} />
        )}
      </div>
      <div style={{ display: accessOpen || (!visible && lastPanel.current === 'access_list') ? 'contents' : 'none' }}>
        {accessMounted && <ProjectAccessPanel projectId={projectId} onClose={closePanel} />}
      </div>
    </WorkspaceAuxiliaryRegion>
  );
}
