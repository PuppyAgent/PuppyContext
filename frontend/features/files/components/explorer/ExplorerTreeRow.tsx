'use client';

import { useExplorerActions } from '@/features/files/explorerSession';

import { Dots } from '@/components/loading';
import { TreeDisclosureMarker } from '@/components/ui/TreeDisclosureMarker';
import {
  ExplorerRowActions,
  getExplorerRowReservedActionWidth,
} from '@/features/files/components/explorer/ExplorerRowActions';
import {
  useIsExpanded,
  usePendingCreatingInfo,
} from '@/features/files/components/explorer/explorerState';
import type { ExplorerSidebarProps, MillerColumnItem } from '@/features/files/components/explorer/types';
import type { ContentType } from '@/features/files/components/views/GridView';
import type { FileImportTarget } from '@/features/files/hooks/useFileImport';
import { directChildrenOf } from '@/lib/contentTreeApi';
import {
  resolveDataTransferSnapshot,
  snapshotDataTransfer,
} from '@/lib/dropFiles';
import { FileGlyphIcon } from '@/lib/fileIcons';
import { useExplorerTreeDir } from '@/lib/hooks/useData';
import { useNodeDrop } from '@/lib/hooks/useNodeDrop';
import {
  getNodeTypeConfig,
  getSyncSource,
  getSyncSourceIcon,
  isFolderType,
  isSyncedType,
} from '@/lib/nodeTypeConfig';
import {
  SIDEBAR_META_TYPOGRAPHY,
  SIDEBAR_ROW_TYPOGRAPHY,
} from '@/lib/uiTypography';
import type {
  CSSProperties,
  DragEvent as ReactDragEvent,
  MouseEvent as ReactMouseEvent,
  ReactNode,
} from 'react';
import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';

const FILE_DROP_TARGET_BG = 'var(--po-selected)';
const FILE_DROP_SCOPE_BG = 'var(--po-hover)';
const FILE_DROP_TARGET_BORDER =
  'color-mix(in srgb, var(--po-text) 24%, transparent)';
const FILE_DROP_SCOPE_BORDER = 'var(--po-border-strong)';

// Row geometry is intentionally shared with the other secondary
// sidebars. Lines are a visual overlay only; do not solve line gaps by
// changing the row height, font size, or padding.
export const EXPLORER_TREE_ROW_HEIGHT = 32;
export const EXPLORER_TREE_ROW_GAP = 2;
export const EXPLORER_TREE_INDENT = 16;
export const EXPLORER_TREE_ROW_MARGIN_X = 6;
export const EXPLORER_TREE_CONTENT_INSET = 8;
export const EXPLORER_TREE_ROW_MARGIN_Y = EXPLORER_TREE_ROW_GAP / 2;
const EXPLORER_TREE_LINE_OVERDRAW = 2;
const EXPLORER_TREE_META_OFFSET = 14;
const SUBTREE_MOTION_MIN_MS = 170;
const SUBTREE_MOTION_MAX_MS = 340;
const SUBTREE_MOTION_PX_FACTOR = 0.28;
const SUBTREE_MOTION_EASE = 'cubic-bezier(0.25, 0.1, 0.25, 1)';

function getFolderLoadErrorLabel(error: unknown): string {
  const detail =
    typeof error === 'object' && error !== null && 'detail' in error
      ? (error as { detail?: unknown }).detail
      : undefined;
  const detailCode =
    typeof detail === 'object' && detail !== null && 'code' in detail
      ? String((detail as { code?: unknown }).code)
      : undefined;
  const maybeStatus =
    typeof error === 'object' && error !== null && 'status' in error
      ? Number((error as { status?: unknown }).status)
      : undefined;
  const message = error instanceof Error ? error.message : '';

  if (detailCode === 'DIRECTORY_NOT_FOUND') return 'Folder not found';
  if (detailCode === 'VERSION_STORAGE_INTEGRITY_ERROR') return 'Damaged folder';
  if (maybeStatus === 401 || maybeStatus === 403) return 'No access';
  if (maybeStatus === 404) return 'Folder not found';
  if (
    maybeStatus === 500 ||
    message.toLowerCase().includes('storage integrity') ||
    message.toLowerCase().includes('object not found')
  ) {
    return 'Damaged folder';
  }
  return 'Failed to load folder';
}

function getFolderLoadErrorTitle(error: unknown): string | undefined {
  return error instanceof Error && error.message ? error.message : undefined;
}

function hasExternalFiles(event: ReactDragEvent): boolean {
  return Array.from(event.dataTransfer.types).includes('Files');
}

function getParentFileDropTarget(item: MillerColumnItem): FileImportTarget {
  if (!item.id.includes('/')) return { path: null, name: 'Root' };
  const parentPath = item.id.split('/').slice(0, -1).join('/');
  return {
    path: parentPath,
    name: parentPath.split('/').pop() || 'Root',
  };
}

export const FolderIcon = ({ expanded }: { expanded?: boolean }) => {
  return <TreeDisclosureMarker expanded={expanded} />;
};

type SidebarFileKind =
  | 'json'
  | 'markdown'
  | 'html'
  | 'pdf'
  | 'image'
  | 'audio'
  | 'video'
  | 'spreadsheet'
  | 'archive'
  | 'code'
  | 'text'
  | 'file';

const FILE_KIND_TITLES: Record<SidebarFileKind, string> = {
  json: 'JSON file',
  markdown: 'Markdown file',
  html: 'HTML file',
  pdf: 'PDF file',
  image: 'Image file',
  audio: 'Audio file',
  video: 'Video file',
  spreadsheet: 'Spreadsheet file',
  archive: 'Archive file',
  code: 'Code file',
  text: 'Text file',
  file: 'File',
};

const FILE_ICON_STROKE = 'var(--po-file-icon-stroke)';
const FILE_ICON_SOFT = 'var(--po-text-subtle)';
const FILE_ICON_FILL =
  'color-mix(in srgb, var(--po-file-icon-body) 65%, transparent)';

const SETI_FILE_COLORS: Record<SidebarFileKind, string> = {
  json: 'var(--po-file-accent-json)',
  markdown: 'var(--po-file-accent-markdown)',
  html: 'var(--po-file-accent-html)',
  pdf: 'var(--po-file-accent-pdf)',
  image: 'var(--po-file-accent-image)',
  audio: 'var(--po-file-accent-audio)',
  video: 'var(--po-file-accent-video)',
  spreadsheet: 'var(--po-file-accent-sheet)',
  archive: 'var(--po-file-accent-pdf)',
  code: 'var(--po-file-accent-code)',
  text: 'var(--po-file-accent-default)',
  file: 'var(--po-file-accent-default)',
};

const EXTENSION_KIND: Record<string, SidebarFileKind> = {
  json: 'json',
  jsonl: 'json',
  md: 'markdown',
  mdx: 'markdown',
  markdown: 'markdown',
  html: 'html',
  htm: 'html',
  xhtml: 'html',
  pdf: 'pdf',
  jpg: 'image',
  jpeg: 'image',
  png: 'image',
  gif: 'image',
  webp: 'image',
  svg: 'image',
  avif: 'image',
  heic: 'image',
  mp3: 'audio',
  wav: 'audio',
  m4a: 'audio',
  aac: 'audio',
  flac: 'audio',
  ogg: 'audio',
  opus: 'audio',
  mp4: 'video',
  mov: 'video',
  webm: 'video',
  avi: 'video',
  mkv: 'video',
  csv: 'spreadsheet',
  tsv: 'spreadsheet',
  xls: 'spreadsheet',
  xlsx: 'spreadsheet',
  zip: 'archive',
  rar: 'archive',
  '7z': 'archive',
  tar: 'archive',
  gz: 'archive',
  tgz: 'archive',
  js: 'code',
  jsx: 'code',
  ts: 'code',
  tsx: 'code',
  css: 'code',
  scss: 'code',
  py: 'code',
  rb: 'code',
  go: 'code',
  rs: 'code',
  java: 'code',
  c: 'code',
  cpp: 'code',
  h: 'code',
  sh: 'code',
  yml: 'code',
  yaml: 'code',
  xml: 'code',
  toml: 'code',
  txt: 'text',
  log: 'text',
};

function getFileExtension(name: string): string | null {
  const match = /\.([^./]{1,12})$/.exec(name.trim());
  return match ? match[1].toLowerCase() : null;
}

function getSidebarFileKind(name: string, type: string): SidebarFileKind {
  const ext = getFileExtension(name);
  if (ext && EXTENSION_KIND[ext]) return EXTENSION_KIND[ext];

  const config = getNodeTypeConfig(type);
  switch (config.iconCategory) {
    case 'json':
      return 'json';
    case 'markdown':
      return 'markdown';
    default:
      break;
  }

  if (type === 'image') return 'image';
  if (type === 'pdf') return 'pdf';
  if (type === 'video') return 'video';
  return 'file';
}

const FILE_KIND_GLYPHS: Record<SidebarFileKind, ReactNode> = {
  audio: (
    <>
      <path
        d='M2.6 10.9V7.1h2.25L8.7 4.35v9.3L4.85 10.9H2.6Z'
        fill={SETI_FILE_COLORS.audio}
      />
      <path
        d='M10.8 6.55c1.05 1.1 1.05 2.8 0 3.9'
        stroke={SETI_FILE_COLORS.audio}
        strokeWidth='1.45'
        strokeLinecap='butt'
      />
      <path
        d='M12.95 5.05c1.8 1.95 1.8 5.9 0 7.9'
        stroke={SETI_FILE_COLORS.audio}
        strokeWidth='1.25'
        strokeLinecap='butt'
        opacity='0.78'
      />
    </>
  ),
  image: (
    <>
      <rect
        x='2.75'
        y='3.75'
        width='12.5'
        height='10.5'
        rx='1.25'
        stroke={SETI_FILE_COLORS.image}
        strokeWidth='1.45'
      />
      <path
        d='M3.8 12.5 6.35 9.65l2.05 2.1 2.35-3.05 3.35 3.8'
        stroke={SETI_FILE_COLORS.image}
        strokeWidth='1.45'
        strokeLinecap='butt'
        strokeLinejoin='miter'
      />
      <rect x='10.85' y='5.6' width='2' height='2' rx='0.35' fill={SETI_FILE_COLORS.image} />
    </>
  ),
  video: (
    <>
      <rect
        x='2.9'
        y='5.1'
        width='12.2'
        height='7.8'
        rx='1.15'
        stroke={SETI_FILE_COLORS.video}
        strokeWidth='1.45'
      />
      <path d='M7.9 7.2v3.6L11.3 9 7.9 7.2Z' fill={SETI_FILE_COLORS.video} />
    </>
  ),
  spreadsheet: (
    <>
      <rect
        x='3.1'
        y='3.1'
        width='11.8'
        height='11.8'
        rx='1.15'
        stroke={SETI_FILE_COLORS.spreadsheet}
        strokeWidth='1.35'
      />
      <path
        d='M3.35 7.05h11.3M3.35 10.95h11.3M7.05 3.35v11.3M10.95 3.35v11.3'
        stroke={SETI_FILE_COLORS.spreadsheet}
        strokeWidth='1.05'
        opacity='0.82'
      />
    </>
  ),
  json: (
    <text
      x='9'
      y='12.35'
      textAnchor='middle'
      fontSize='9.5'
      fontWeight='800'
      fontFamily='var(--po-font-sans)'
      fill={SETI_FILE_COLORS.json}
    >
      {'{}'}
    </text>
  ),
  html: (
    <>
      <path
        d='m7.05 5.15-3.5 3.75 3.5 3.75'
        stroke={SETI_FILE_COLORS.html}
        strokeWidth='1.65'
        strokeLinecap='butt'
        strokeLinejoin='miter'
      />
      <path
        d='m10.95 5.15 3.5 3.75-3.5 3.75'
        stroke={SETI_FILE_COLORS.html}
        strokeWidth='1.65'
        strokeLinecap='butt'
        strokeLinejoin='miter'
      />
      <path
        d='M9.95 4.95 8.05 12.9'
        stroke={SETI_FILE_COLORS.html}
        strokeWidth='1.35'
        strokeLinecap='butt'
        opacity='0.86'
      />
    </>
  ),
  code: (
    <>
      <path
        d='m7.2 5.55-3 3.45 3 3.45'
        stroke={SETI_FILE_COLORS.code}
        strokeWidth='1.55'
        strokeLinecap='butt'
        strokeLinejoin='miter'
      />
      <path
        d='m10.8 5.55 3 3.45-3 3.45'
        stroke={SETI_FILE_COLORS.code}
        strokeWidth='1.55'
        strokeLinecap='butt'
        strokeLinejoin='miter'
      />
    </>
  ),
  pdf: (
    <>
      <path
        d='M5.15 2.75h5.6l2.6 2.65v8.5c0 .5-.4.9-.9.9h-7.3c-.5 0-.9-.4-.9-.9V3.65c0-.5.4-.9.9-.9Z'
        fill={FILE_ICON_FILL}
        stroke={SETI_FILE_COLORS.pdf}
        strokeWidth='1.3'
        strokeLinejoin='round'
      />
      <path
        d='M10.75 2.95v2.45h2.4'
        stroke={SETI_FILE_COLORS.pdf}
        strokeWidth='1.05'
        strokeLinejoin='round'
      />
      <path
        d='M5.95 10.1h5.85M5.95 12.15h4.1'
        stroke={SETI_FILE_COLORS.pdf}
        strokeWidth='1.15'
        strokeLinecap='round'
      />
    </>
  ),
  markdown: (
    <>
      <path
        d='M5.15 2.75h5.6l2.6 2.65v8.5c0 .5-.4.9-.9.9h-7.3c-.5 0-.9-.4-.9-.9V3.65c0-.5.4-.9.9-.9Z'
        fill={FILE_ICON_FILL}
        stroke={SETI_FILE_COLORS.markdown}
        strokeWidth='1.3'
        strokeLinejoin='miter'
      />
      <path
        d='M10.75 2.95v2.45h2.4'
        stroke={SETI_FILE_COLORS.markdown}
        strokeWidth='1.05'
        strokeLinejoin='miter'
      />
      <text
        x='8.8'
        y='12.3'
        textAnchor='middle'
        fontSize='7.6'
        fontWeight='780'
        fontFamily='var(--po-font-sans)'
        fill={SETI_FILE_COLORS.markdown}
      >
        M
      </text>
    </>
  ),
  archive: (
    <>
      <path
        d='M4 6.2 6.1 4.2h5.8L14 6.2v6.6c0 .6-.5 1.1-1.1 1.1H5.1c-.6 0-1.1-.5-1.1-1.1V6.2Z'
        fill={FILE_ICON_FILL}
        stroke={SETI_FILE_COLORS.archive}
        strokeWidth='1.25'
        strokeLinejoin='round'
      />
      <path
        d='M6.2 7.2h5.6M7.3 9.1h3.4M7.3 11h3.4'
        stroke={SETI_FILE_COLORS.archive}
        strokeWidth='1.05'
        strokeLinecap='round'
        opacity='0.82'
      />
    </>
  ),
  text: (
    <>
      <path
        d='M5.1 2.75h5.65l2.6 2.65v8.5c0 .5-.4.9-.9.9h-7.35c-.5 0-.9-.4-.9-.9V3.65c0-.5.4-.9.9-.9Z'
        fill={FILE_ICON_FILL}
        stroke={SETI_FILE_COLORS.text}
        strokeWidth='1.2'
        strokeLinejoin='round'
      />
      <path
        d='M10.75 2.95v2.45h2.4'
        stroke={FILE_ICON_SOFT}
        strokeWidth='1'
        strokeLinejoin='round'
      />
      <path
        d='M5.85 8.25h5.2M5.85 10.25h5.2M5.85 12.25h3.65'
        stroke={SETI_FILE_COLORS.text}
        strokeWidth='1.05'
        strokeLinecap='round'
      />
    </>
  ),
  file: (
    <>
      <path
        d='M5.1 2.75h5.65l2.6 2.65v8.5c0 .5-.4.9-.9.9h-7.35c-.5 0-.9-.4-.9-.9V3.65c0-.5.4-.9.9-.9Z'
        fill={FILE_ICON_FILL}
        stroke={SETI_FILE_COLORS.file}
        strokeWidth='1.2'
        strokeLinejoin='round'
      />
      <path
        d='M10.75 2.95v2.45h2.4'
        stroke={FILE_ICON_SOFT}
        strokeWidth='1'
        strokeLinejoin='round'
      />
    </>
  ),
};

export function FileTypeIcon({
  kind,
  size = 16,
}: {
  kind: SidebarFileKind;
  size?: number;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox='0 0 18 18'
      fill='none'
      aria-hidden='true'
    >
      <title>{FILE_KIND_TITLES[kind]}</title>

      {FILE_KIND_GLYPHS[kind] ?? FILE_KIND_GLYPHS.file}
    </svg>
  );
}

function FileIcon({
  name,
  type,
  syncSource,
  iconSize,
}: {
  name: string;
  type: string;
  syncSource?: string | null;
  iconSize?: number;
}) {
  const config = getNodeTypeConfig(type);
  const actualSource = syncSource || getSyncSource(type);
  const BadgeIcon = getSyncSourceIcon(actualSource) || config.badgeIcon;
  const size = iconSize ?? 18;

  if (BadgeIcon) return <BadgeIcon size={size} />;

  return <FileGlyphIcon name={name} type={type} size={size} />;
}

function getSyncDirectionArrow(
  type: string,
  direction: 'inbound' | 'outbound' | 'bidirectional' = 'inbound'
): string | null {
  if (!isSyncedType(type)) return null;
  if (direction === 'bidirectional') return ' ⇄';
  if (direction === 'outbound') return ' ←';
  return ' →';
}

function getTypeExtension(type: string): string | null {
  const config = getNodeTypeConfig(type);

  switch (config.iconCategory) {
    case 'json':
      return '.json';
    case 'markdown':
      return '.md';
    default:
      return null;
  }
}

function hasFileExtension(name: string): boolean {
  return /\.\w{1,10}$/.test(name);
}

function TreeIndentGuide({ depth }: { depth: number }) {
  if (depth <= 0) return null;

  return (
    <div
      aria-hidden
      style={{
        position: 'absolute',
        left: depth * EXPLORER_TREE_INDENT,
        top: -EXPLORER_TREE_LINE_OVERDRAW,
        bottom: -EXPLORER_TREE_LINE_OVERDRAW,
        width: 1,
        background: 'var(--po-tree-guide)',
        pointerEvents: 'none',
        zIndex: 0,
      }}
    />
  );
}

export function ExplorerTreeMetaRow({
  depth,
  children,
}: {
  depth: number;
  children: ReactNode;
}) {
  return (
    <div
      style={{
        height: EXPLORER_TREE_ROW_HEIGHT,
        margin: `${EXPLORER_TREE_ROW_MARGIN_Y}px ${EXPLORER_TREE_ROW_MARGIN_X}px`,
        paddingLeft:
          EXPLORER_TREE_CONTENT_INSET +
          depth * EXPLORER_TREE_INDENT +
          EXPLORER_TREE_META_OFFSET,
        display: 'flex',
        alignItems: 'center',
        boxSizing: 'border-box',
        position: 'relative',
      }}
    >
      <TreeIndentGuide depth={depth} />
      <div
        style={{
          position: 'relative',
          zIndex: 1,
          display: 'flex',
          alignItems: 'center',
        }}
      >
        {children}
      </div>
    </div>
  );
}

function getSubtreeMotionDurationMs(
  fromHeight: number,
  toHeight: number
): number {
  const distance = Math.abs(toHeight - fromHeight);
  return Math.round(
    Math.min(
      SUBTREE_MOTION_MAX_MS,
      Math.max(
        SUBTREE_MOTION_MIN_MS,
        SUBTREE_MOTION_MIN_MS + distance * SUBTREE_MOTION_PX_FACTOR
      )
    )
  );
}

function ExplorerSubtreeMotion({
  visible,
  onExited,
  children,
}: {
  visible: boolean;
  onExited: () => void;
  children: ReactNode;
}) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const rafRef = useRef<number | null>(null);
  const onExitedRef = useRef(onExited);
  const [height, setHeight] = useState<number | 'auto'>(0);
  const [durationMs, setDurationMs] = useState(SUBTREE_MOTION_MIN_MS);

  const cancelFrame = useCallback(() => {
    if (rafRef.current === null) return;
    window.cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
  }, []);

  useEffect(() => cancelFrame, [cancelFrame]);

  useEffect(() => {
    onExitedRef.current = onExited;
  }, [onExited]);

  useLayoutEffect(() => {
    const wrapper = wrapperRef.current;
    const content = contentRef.current;
    if (!wrapper || !content) return;

    cancelFrame();

    const currentHeight = wrapper.getBoundingClientRect().height;
    const nextHeight = visible ? content.scrollHeight : 0;

    if (!visible && Math.abs(currentHeight - nextHeight) < 1) {
      setHeight(0);
      onExitedRef.current();
      return;
    }

    setDurationMs(getSubtreeMotionDurationMs(currentHeight, nextHeight));
    setHeight(currentHeight);
    rafRef.current = window.requestAnimationFrame(() => {
      setHeight(nextHeight);
      rafRef.current = null;
    });
  }, [cancelFrame, visible]);

  useEffect(() => {
    if (
      !visible ||
      height === 'auto' ||
      typeof ResizeObserver === 'undefined'
    ) {
      return undefined;
    }

    const wrapper = wrapperRef.current;
    const content = contentRef.current;
    if (!wrapper || !content) return undefined;

    let previousHeight = content.scrollHeight;
    const observer = new ResizeObserver(() => {
      const nextHeight = content.scrollHeight;
      if (Math.abs(nextHeight - previousHeight) < 1) return;
      previousHeight = nextHeight;

      cancelFrame();
      const currentHeight = wrapper.getBoundingClientRect().height;
      setDurationMs(getSubtreeMotionDurationMs(currentHeight, nextHeight));
      setHeight(currentHeight);
      rafRef.current = window.requestAnimationFrame(() => {
        setHeight(nextHeight);
        rafRef.current = null;
      });
    });

    observer.observe(content);
    return () => observer.disconnect();
  }, [cancelFrame, height, visible]);

  return (
    <div
      ref={wrapperRef}
      onTransitionEnd={event => {
        if (event.target !== event.currentTarget) return;
        if (event.propertyName !== 'height') return;
        if (!visible) {
          onExitedRef.current();
          return;
        }
        setHeight('auto');
      }}
      style={{
        width: '100%',
        boxSizing: 'border-box',
        height: height === 'auto' ? 'auto' : `${Math.max(0, height)}px`,
        overflow: 'hidden',
        transition: `height ${durationMs}ms ${SUBTREE_MOTION_EASE}`,
        willChange: 'height',
      }}
    >
      <div
        ref={contentRef}
        style={{
          width: '100%',
          minHeight: 0,
          overflow: 'hidden',
          boxSizing: 'border-box',
        }}
      >
        {children}
      </div>
    </div>
  );
}

interface ExplorerTreeRowProps {
  item: MillerColumnItem;
  depth: number;
  projectId: string;
  activeId: string | null;
  onNavigate: (item: MillerColumnItem) => void;
  onCreate?: ExplorerSidebarProps['onCreate'];
  onCreateSync?: ExplorerSidebarProps['onCreateSync'];
  onOpenAccess?: ExplorerSidebarProps['onOpenAccess'];
  endpointByNodeId?: ExplorerSidebarProps['endpointByNodeId'];
  onRename?: ExplorerSidebarProps['onRename'];
  onDelete?: ExplorerSidebarProps['onDelete'];
  onDownload?: ExplorerSidebarProps['onDownload'];
  onFilesDrop?: ExplorerSidebarProps['onFilesDrop'];
  onMoveNode?: ExplorerSidebarProps['onMoveNode'];
  activeSyncNodeId?: string | null;
  highlightNodeId?: string | null;
  highlightVariant?: ExplorerSidebarProps['highlightVariant'];
  createMenuOpenForId?: string | null;
  createMenuOpenAction?: ExplorerSidebarProps['createMenuOpenAction'];
  activeFileDropTargetPath?: string | null;
  onFileDragTarget?: (target: FileImportTarget | null) => void;
}

export const ExplorerTreeRow = memo(function ExplorerTreeRow({
  item,
  depth,
  projectId,
  activeId,
  onNavigate,
  onCreate,
  onCreateSync,
  onOpenAccess,
  endpointByNodeId,
  onRename,
  onDelete,
  onDownload,
  onFilesDrop,
  onMoveNode,
  activeSyncNodeId,
  highlightNodeId,
  highlightVariant = 'default',
  createMenuOpenForId,
  createMenuOpenAction,
  activeFileDropTargetPath,
  onFileDragTarget,
}: ExplorerTreeRowProps) {
  const { toggleExpanded } = useExplorerActions();
  const isFolder = isFolderType(item.type);
  const isDamagedNode = item.integrity_status === 'damaged';
  const isSynced = item.is_synced;
  const isExpanded = useIsExpanded(projectId, item.id);
  const pendingCreatingInfo = usePendingCreatingInfo(projectId, item.id);
  const isPendingCreating = pendingCreatingInfo !== null;
  const pendingCreatingLabel = pendingCreatingInfo?.label ?? 'Creating';
  const expanded = isExpanded && isFolder && !isPendingCreating;
  const [renderSubtree, setRenderSubtree] = useState(expanded);
  const rowRef = useRef<HTMLDivElement>(null);
  const [isHovered, setIsHovered] = useState(false);
  const isHighlighted = !isPendingCreating && highlightNodeId === item.id;
  const openMenuAction =
    createMenuOpenForId === item.id ? (createMenuOpenAction ?? null) : null;
  const isAnyCreateMenuOpen = openMenuAction !== null;
  const endpoints = endpointByNodeId?.get(item.id) ?? [];
  const hasConfiguredAccess =
    isFolder && !isPendingCreating && endpoints.length > 0 && !!onOpenAccess;
  const shouldRenderSubtree = expanded || renderSubtree;

  const { isDropTarget, dropHandlers } = useNodeDrop({
    targetFolderId: item.id,
    onMoveNode,
    disabled: !isFolder || isPendingCreating,
  });
  const fileDropTarget = useMemo(() => isFolder
    ? { path: item.id, name: item.name }
    : getParentFileDropTarget(item), [isFolder, item]);

  useEffect(() => {
    if (isHighlighted && rowRef.current) {
      rowRef.current.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, [isHighlighted]);

  useLayoutEffect(() => {
    if (expanded) {
      setRenderSubtree(true);
    }
  }, [expanded]);

  const {
    nodes: children,
    isLoading: loading,
    isValidating,
    error: loadError,
    refresh,
  } = useExplorerTreeDir(
    shouldRenderSubtree ? projectId : '',
    shouldRenderSubtree ? item.id : undefined
  );

  const handleClick = useCallback(
    (e: ReactMouseEvent) => {
      e.stopPropagation();
      if (isPendingCreating) return;
      if (isFolder) {
        toggleExpanded(projectId, item.id);
        return;
      }
      onNavigate(item);
    },
    [isFolder, isPendingCreating, item, onNavigate, projectId]
  );

  const handleToggleExpand = useCallback(
    (e: ReactMouseEvent) => {
      e.stopPropagation();
      if (isPendingCreating) return;
      toggleExpanded(projectId, item.id);
    },
    [isPendingCreating, item.id, projectId]
  );

  const isActive = activeId === item.id;
  const isSyncActive = activeSyncNodeId === item.id;
  const isRowActive = !isPendingCreating && (isActive || isSyncActive);
  const isAccessPointHighlight =
    isHighlighted && highlightVariant === 'access-point';
  const isFileDropTarget = isFolder && activeFileDropTargetPath === item.id;
  const isInsideActiveFileDropScope =
    !!activeFileDropTargetPath &&
    item.id.startsWith(`${activeFileDropTargetPath}/`);
  const hasSpecialBg =
    isDropTarget ||
    isFileDropTarget ||
    isInsideActiveFileDropScope ||
    isHighlighted ||
    isRowActive ||
    isAnyCreateMenuOpen;
  const staticBg =
    isDropTarget || isFileDropTarget
      ? FILE_DROP_TARGET_BG
      : isInsideActiveFileDropScope
        ? FILE_DROP_SCOPE_BG
        : isHighlighted
          ? isAccessPointHighlight
            ? 'color-mix(in srgb, var(--po-success) 14%, transparent)'
            : 'var(--po-selected)'
          : isRowActive || isAnyCreateMenuOpen
            ? // Translucent overlay rather than opaque var(--po-border) — the
              // earlier opaque colour was almost identical to the tree
              // line (var(--po-tree-guide) vs var(--po-border), a 3-unit-per-channel delta) AND
              // it covered the parent's continuation line that runs
              // through the row, so a selected leaf looked visually
              // disconnected from its sibling chain. An rgba overlay keeps
              // the lines showing through naturally and stays consistent
              // with every other "special" state (hover, drop target,
              // search highlight) which all already use rgba.
              'var(--po-selected)'
            : 'transparent';
  const staticColor =
    isDropTarget || isFileDropTarget
      ? 'var(--po-text)'
      : isAccessPointHighlight
        ? 'var(--po-success)'
        : isRowActive || isAnyCreateMenuOpen
          ? 'var(--po-text)'
          : 'var(--po-text-muted)';
  const isSoftHovered = isHovered && !hasSpecialBg;
  const rowBackground = isSoftHovered ? 'var(--po-hover)' : staticBg;
  const rowColor = isPendingCreating
    ? 'var(--po-text-subtle)'
    : isSoftHovered
      ? 'var(--po-text-muted)'
      : staticColor;

  const childItems: MillerColumnItem[] = useMemo(
    () =>
      directChildrenOf(children, item.id).map(node => ({
        id: node.id,
        name: node.name,
        type: node.type as ContentType,
        is_synced: node.is_synced,
        sync_source: node.sync_source,
        last_synced_at: node.last_synced_at,
        integrity_status: node.integrity_status,
      })),
    [children, item.id]
  );
  const isChildLoadInFlight =
    (loading || isValidating || isPendingCreating) && childItems.length === 0;

  const hasObjectMenu =
    !isPendingCreating &&
    !!(onRename || onDelete || onDownload || (isFolder && onCreateSync));
  const hasInlineActions =
    !isPendingCreating &&
    !!(hasObjectMenu || (isFolder && (onCreate || hasConfiguredAccess)));
  const reservedActionWidth = hasInlineActions
    ? getExplorerRowReservedActionWidth(hasConfiguredAccess, hasObjectMenu)
    : 0;
  const touchActionCount = Number(hasObjectMenu) + Number(isFolder && Boolean(onCreate)) + Number(hasConfiguredAccess);

  const activateFileDropTarget = useCallback(
    (event: ReactDragEvent<HTMLDivElement>) => {
      if (isPendingCreating) return false;
      if (!hasExternalFiles(event)) return false;
      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = 'copy';
      onFileDragTarget?.(fileDropTarget);
      return true;
    },
    [fileDropTarget, isPendingCreating, onFileDragTarget]
  );

  const handleExternalFileDrop = useCallback(
    (event: ReactDragEvent<HTMLDivElement>) => {
      if (!hasExternalFiles(event)) return false;
      event.preventDefault();
      event.stopPropagation();

      // Snapshot synchronously — see lib/dropFiles.ts. Reading
      // ``dataTransfer.files`` directly would treat a dropped folder
      // as a single 0-byte "file" and silently lose every real file
      // inside.
      const snapshot = snapshotDataTransfer(event.nativeEvent);
      void resolveDataTransferSnapshot(snapshot).then(files => {
        if (files.length > 0) {
          onFilesDrop?.(files, fileDropTarget);
        }
      });
      onFileDragTarget?.(null);
      return true;
    },
    [fileDropTarget, onFileDragTarget, onFilesDrop]
  );

  return (
    <div
      style={{ position: 'relative', width: '100%', boxSizing: 'border-box' }}
    >
      <div
        ref={rowRef}
        data-menu-host='true'
        className='workspace-explorer-row group/row'
        onClick={handleClick}
        onMouseEnter={() => setIsHovered(true)}
        onMouseLeave={() => setIsHovered(false)}
        // Any product node can be a move source. Only folder rows become
        // drop targets, so this enables file -> folder moves without allowing
        // file -> file drops.
        draggable={Boolean(onMoveNode) && !isPendingCreating}
        onDragStart={e => {
          if (!onMoveNode || isPendingCreating) {
            e.preventDefault();
            return;
          }
          e.dataTransfer.setData(
            'application/x-puppyone-node',
            JSON.stringify({
              id: item.id,
              name: item.name,
              type: item.type,
              parentId: item.id.includes('/')
                ? item.id.split('/').slice(0, -1).join('/')
                : null,
            })
          );
          e.dataTransfer.effectAllowed = 'move';
        }}
        onDragEnter={e => {
          if (!activateFileDropTarget(e)) dropHandlers.onDragEnter(e);
        }}
        onDragOver={e => {
          if (!activateFileDropTarget(e)) dropHandlers.onDragOver(e);
        }}
        onDragLeave={e => {
          dropHandlers.onDragLeave(e);
        }}
        onDrop={e => {
          if (!handleExternalFileDrop(e)) dropHandlers.onDrop(e);
        }}
        style={{
          display: 'flex',
          alignItems: 'center',
          margin: `${EXPLORER_TREE_ROW_MARGIN_Y}px ${EXPLORER_TREE_ROW_MARGIN_X}px`,
          height: EXPLORER_TREE_ROW_HEIGHT,
          boxSizing: 'border-box',
          borderRadius: 6,
          background: rowBackground,
          color: rowColor,
          ...SIDEBAR_ROW_TYPOGRAPHY,
          userSelect: 'none',
          transition: 'background 0.14s, color 0.14s, opacity 0.18s',
          boxShadow:
            isDropTarget || isFileDropTarget
              ? `inset 0 0 0 1px ${FILE_DROP_TARGET_BORDER}`
              : isInsideActiveFileDropScope
                ? `inset 1px 0 0 0 ${FILE_DROP_SCOPE_BORDER}`
                : isAccessPointHighlight
                  ? 'inset 2px 0 0 0 color-mix(in srgb, var(--po-success) 90%, transparent)'
                  : 'none',
          opacity: isPendingCreating ? 0.92 : 1,
          cursor: isPendingCreating ? 'progress' : 'pointer',
          position: 'relative',
        }}
      >
        {/* VS Code-style indent guide for this depth: a straight
            vertical rail only, with no per-row horizontal elbow. */}
        <TreeIndentGuide depth={depth} />

        <div
          data-explorer-label=''
          style={{
            '--workspace-row-action-width': `${hasInlineActions ? touchActionCount * 46 + 6 : 6}px`,
            flex: 1,
            minWidth: 0,
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            height: '100%',
            boxSizing: 'border-box',
            paddingLeft:
              EXPLORER_TREE_CONTENT_INSET + depth * EXPLORER_TREE_INDENT,
            paddingRight: reservedActionWidth > 0 ? reservedActionWidth + 6 : 6,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          } as CSSProperties}
        >
          <div
            onClick={isFolder ? handleToggleExpand : undefined}
            style={{
              display: 'flex',
              alignItems: 'center',
              flexShrink: 0,
              width: 18,
              height: 18,
              justifyContent: 'center',
              position: 'relative',
            }}
          >
            {isFolder && isPendingCreating ? (
              <div className='flex items-center justify-center'>
                <FolderIcon expanded={false} />
              </div>
            ) : isFolder ? (
              <FolderIcon expanded={expanded} />
            ) : (
              (() => {
                const arrow = getSyncDirectionArrow(item.type);

                if (arrow) {
                  return (
                    <div
                      style={{ display: 'flex', alignItems: 'center', gap: 0 }}
                    >
                      <FileIcon
                        name={item.name}
                        type={item.type}
                        syncSource={item.sync_source}
                        iconSize={10}
                      />
                      <span
                        style={{
                          color: 'var(--po-text-subtle)',
                          fontSize: 7,
                          lineHeight: 1,
                        }}
                      >
                        {arrow}
                      </span>
                    </div>
                  );
                }

                return (
                  <FileIcon
                    name={item.name}
                    type={item.type}
                    syncSource={item.sync_source}
                  />
                );
              })()
            )}
          </div>

          <span
            style={{
              flex: 1,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              minWidth: 0,
              fontStyle: 'normal',
            }}
          >
            {isPendingCreating ? (
              <span
                title={pendingCreatingLabel}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 6,
                  maxWidth: '100%',
                  minWidth: 0,
                }}
              >
                <span
                  style={{
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                  }}
                >
                  {pendingCreatingLabel}
                </span>
                <Dots
                  size='xs'
                  ariaLabel={pendingCreatingLabel}
                  style={{
                    flexShrink: 0,
                    color: 'var(--po-text-muted)',
                  }}
                />
              </span>
            ) : (
              <>
                {item.name}
                {!isFolder &&
                  !hasFileExtension(item.name) &&
                  (() => {
                    const ext = getTypeExtension(item.type);
                    return ext ? (
                      <span
                        style={{
                          color: 'var(--po-text-disabled)',
                          fontSize: 11,
                        }}
                      >
                        {ext}
                      </span>
                    ) : null;
                  })()}
              </>
            )}
          </span>

          {isDamagedNode && (
            <span
              title='This historical entry points at a missing Git object. You can delete it to repair the tree.'
              style={{
                ...SIDEBAR_META_TYPOGRAPHY,
                color: 'var(--po-danger)',
                flexShrink: 0,
              }}
            >
              Damaged
            </span>
          )}

          {hasInlineActions && (
            <ExplorerRowActions
              nodeId={item.id}
              createParentId={item.id}
              accessPath={item.id}
              isFolder={isFolder}
              endpoints={endpoints}
              openMenuAction={openMenuAction}
              isSynced={isSynced}
              itemName={item.name}
              onCreate={onCreate}
              onCreateSync={onCreateSync}
              onOpenAccess={onOpenAccess}
              onRename={onRename}
              onDelete={onDelete}
              onDownload={onDownload}
            />
          )}
        </div>
      </div>

      {shouldRenderSubtree && (
        <ExplorerSubtreeMotion
          visible={expanded}
          onExited={() => setRenderSubtree(false)}
        >
          <div
            style={{
              position: 'relative',
              width: '100%',
              boxSizing: 'border-box',
            }}
          >
            {/* The parent indent guide spans the full expanded subtree,
              including loading/error/empty meta rows. This matches
              VS Code's continuous guide behavior instead of closing
              the line at the last sibling row. */}
            {depth > 0 && (
              <div
                aria-hidden
                style={{
                  position: 'absolute',
                  left:
                    EXPLORER_TREE_ROW_MARGIN_X + depth * EXPLORER_TREE_INDENT,
                  top: 0,
                  bottom: 0,
                  width: 1,
                  background: 'var(--po-tree-guide)',
                  pointerEvents: 'none',
                  zIndex: 0,
                }}
              />
            )}
            {isChildLoadInFlight ? (
              <ExplorerTreeMetaRow depth={depth + 1}>
                <Dots size='xs' />
              </ExplorerTreeMetaRow>
            ) : childItems.length > 0 ? (
              childItems.map(child => (
                <ExplorerTreeRow
                  key={child.id}
                  item={child}
                  depth={depth + 1}
                  projectId={projectId}
                  activeId={activeId}
                  onNavigate={onNavigate}
                  onCreate={onCreate}
                  onCreateSync={onCreateSync}
                  onOpenAccess={onOpenAccess}
                  endpointByNodeId={endpointByNodeId}
                  onRename={onRename}
                  onDelete={onDelete}
                  onDownload={onDownload}
                  onFilesDrop={onFilesDrop}
                  onMoveNode={onMoveNode}
                  activeSyncNodeId={activeSyncNodeId}
                  highlightNodeId={highlightNodeId}
                  highlightVariant={highlightVariant}
                  createMenuOpenForId={createMenuOpenForId}
                  createMenuOpenAction={createMenuOpenAction}
                  activeFileDropTargetPath={activeFileDropTargetPath}
                  onFileDragTarget={onFileDragTarget}
                />
              ))
            ) : !isPendingCreating && loadError ? (
              <ExplorerTreeMetaRow depth={depth + 1}>
                <span
                  title={getFolderLoadErrorTitle(loadError)}
                  style={{
                    ...SIDEBAR_META_TYPOGRAPHY,
                    color: 'var(--po-danger)',
                  }}
                >
                  {getFolderLoadErrorLabel(loadError)}
                  {' '}<button type='button' className='underline' onClick={() => { void refresh().catch(() => {}); }}>Retry</button>
                </span>
              </ExplorerTreeMetaRow>
            ) : !loading ? (
              <ExplorerTreeMetaRow depth={depth + 1}>
                <span
                  style={{
                    ...SIDEBAR_META_TYPOGRAPHY,
                    color: 'var(--po-text-disabled)',
                  }}
                >
                  Empty folder
                </span>
              </ExplorerTreeMetaRow>
            ) : null}
          </div>
        </ExplorerSubtreeMotion>
      )}
    </div>
  );
});
