import { act, fireEvent, render, screen } from '@testing-library/react';
import { StrictMode, useEffect, useState } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { ResponsiveWorkspaceProvider } from '@/features/workspace/responsive';
import { ProjectSessionProvider, useProjectSession } from '@/features/workspace/session';
import { createWorkspaceLayoutStore } from '@/features/workspace/layoutStore';
import { useWorkspaceRegions } from '@/features/workspace/regions';
import { AgentChatHeader } from '@/components/chat/AgentChatChrome';
import { ProjectAuxiliarySidebar } from '@/app/(main)/projects/[projectId]/_components/ProjectAuxiliarySidebar';

const runtime = vi.hoisted(() => ({ renders: 0, mounts: 0, disconnects: 0 }));
vi.mock('@/features/agent/components/ProjectChatPanel', () => ({
  ProjectChatPanel: ({ onClose }: { onClose: () => void }) => {
    runtime.renders++;
    useEffect(() => { runtime.mounts++; return () => { runtime.disconnects++; }; }, []);
    const [draft, setDraft] = useState('');
    return <><AgentChatHeader title='New chat' onClose={onClose} /><textarea aria-label='Chat draft' value={draft} onChange={event => setDraft(event.target.value)} /></>;
  },
}));
afterEach(() => vi.unstubAllGlobals());

function Workspace() {
  const regions = useWorkspaceRegions();
  const open = useProjectSession(state => state.openPanel);
  return <div className='workspace-project-layout'><header ref={regions.header} data-workspace-header><button data-workspace-chat-trigger onClick={() => open({ type: 'workspace_chat' })}>Chat</button></header><div ref={regions.projectBody} className='workspace-primary-body' data-testid='document'>Document stays mounted</div><ProjectAuxiliarySidebar projectId='p' /></div>;
}

it('preserves the actual auxiliary chat instance and desktop width across folding and closing', () => {
  Object.assign(runtime, { renders: 0, mounts: 0, disconnects: 0 });
  const layoutStore = createWorkspaceLayoutStore({ availableWidth: 390 });
  render(<ResponsiveWorkspaceProvider layoutStore={layoutStore}><ProjectSessionProvider projectId='p'><Workspace /></ProjectSessionProvider></ResponsiveWorkspaceProvider>);
  fireEvent.click(screen.getByText('Chat'));
  const draft = screen.getByLabelText('Chat draft');
  expect(document.activeElement).toBe(screen.getByLabelText('Close chat panel'));
  expect(screen.getByText('Chat').closest('[inert]')).toBeNull();
  expect(screen.getByTestId('document').hasAttribute('inert')).toBe(true);
  fireEvent.change(draft, { target: { value: 'Continue reviewing this file' } });
  const before = runtime.renders;
  for (const next of [820, 390, 1280]) {
    act(() => layoutStore.getState().setEnvironment({ availableWidth: next, segments: null }));
    expect(runtime.renders).toBe(before);
    expect(runtime.mounts).toBe(1);
    expect(runtime.disconnects).toBe(0);
    expect(screen.getByLabelText('Chat draft')).toBe(draft);
    expect((draft as HTMLTextAreaElement).value).toBe('Continue reviewing this file');
    expect(screen.getByTestId('document').hasAttribute('inert')).toBe(next < 620);
  }
  expect((document.querySelector('.workspace-auxiliary') as HTMLElement).style.getPropertyValue('--panel-width')).toBe('450px');
  fireEvent.keyDown(screen.getByRole('separator', { name: 'Resize panel' }), { key: 'ArrowLeft' });
  act(() => layoutStore.getState().setEnvironment({ availableWidth: 390, segments: null }));
  act(() => layoutStore.getState().setEnvironment({ availableWidth: 1280, segments: null }));
  expect((document.querySelector('.workspace-auxiliary') as HTMLElement).style.getPropertyValue('--panel-width')).toBe('466px');
  expect(runtime.renders).toBe(before);
  fireEvent.click(screen.getByLabelText('Close chat panel'));
  fireEvent.click(screen.getByText('Chat'));
  expect(screen.getByLabelText('Chat draft')).toBe(draft);
  expect((draft as HTMLTextAreaElement).value).toBe('Continue reviewing this file');
});


it('dismisses the real Agent chrome through its region ref and returns focus to the Header', () => {
  const layoutStore = createWorkspaceLayoutStore({ availableWidth: 390 });
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue([{ width: 20, height: 20 }] as unknown as DOMRectList);
  render(<StrictMode><ResponsiveWorkspaceProvider layoutStore={layoutStore}><ProjectSessionProvider projectId='p'><Workspace /></ProjectSessionProvider></ResponsiveWorkspaceProvider></StrictMode>);
  const trigger = screen.getByText('Chat');
  trigger.focus();
  fireEvent.click(trigger);
  const header = screen.getByText('New chat').closest('header')!;
  const panel = screen.getByRole('complementary', { name: 'Agent sidebar' });
  function pointer(target: HTMLElement, type: string, x: number) {
    const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: 120 });
    Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: 'touch' }, isPrimary: { value: true } });
    fireEvent(target, event);
  }
  pointer(header, 'pointerdown', 150);
  pointer(header, 'pointermove', 170);
  pointer(header, 'lostpointercapture', 170);
  pointer(panel, 'pointermove', 250);
  pointer(panel, 'pointerup', 250);
  expect(screen.queryByRole('complementary', { name: 'Agent sidebar' })).toBeNull();
  expect(screen.getByTestId('document').hasAttribute('inert')).toBe(false);
  expect(document.activeElement).toBe(trigger);
});
