import { createHash } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import {
  DEFAULT_WORKSPACE_INPUT,
  resolveWorkspaceLayout,
  type WorkspaceLayoutInput,
  type WorkspaceSegments,
} from '@/features/workspace/paneLayout';

function workspace(patch: Partial<WorkspaceLayoutInput> = {}): WorkspaceLayoutInput {
  return {
    ...DEFAULT_WORKSPACE_INPUT,
    projects: { ...DEFAULT_WORKSPACE_INPUT.projects },
    files: { ...DEFAULT_WORKSPACE_INPUT.files, present: true, preferredWidth: 320 },
    auxiliary: { ...DEFAULT_WORKSPACE_INPUT.auxiliary, present: true, open: true },
    inspector: { ...DEFAULT_WORKSPACE_INPUT.inspector, present: true, open: true },
    ...patch,
  };
}

const segmentCases: { name: string; segments: WorkspaceSegments | null }[] = [
  { name: 'unsegmented', segments: null },
  { name: 'horizontal', segments: { orientation: 'horizontal', first: 620, second: 420, gap: 20 } },
  { name: 'vertical', segments: { orientation: 'vertical', first: 400, second: 500, gap: 20 } },
  { name: 'empty first segment', segments: { orientation: 'horizontal', first: 0, second: 420, gap: 20 } },
  { name: 'negative second segment', segments: { orientation: 'vertical', first: 400, second: -1, gap: 20 } },
];

describe('workspace layout pre-refactor outputs', () => {
  it.each([1800, 1310, 1309, 1090, 919, 920, 620, 619, 390, 32, 0])(
    'locks every output field at width %s with both right panes requested', availableWidth => {
      expect(resolveWorkspaceLayout(workspace({ availableWidth }))).toMatchSnapshot();
    },
  );

  it.each(segmentCases)('locks $name geometry and retained closed pane widths', ({ segments }) => {
    const open = workspace({ availableWidth: 1060, segments });
    expect(resolveWorkspaceLayout(open)).toMatchSnapshot('open');
    expect(resolveWorkspaceLayout({
      ...open,
      auxiliary: { ...open.auxiliary, open: false },
      inspector: { ...open.inspector, open: false },
    })).toMatchSnapshot('closed');
  });

  it('locks sanitized dimensions and clamped preferences', () => {
    for (const availableWidth of [-1, Number.NaN, Infinity, 0, 319.5, 1280]) {
      const input = workspace({ availableWidth, mainMinWidth: Number.NaN });
      input.projects.preferredWidth = 999;
      input.files.preferredWidth = -10;
      input.auxiliary.preferredWidth = Infinity;
      input.inspector.preferredWidth = 999;
      expect(resolveWorkspaceLayout(input)).toMatchSnapshot(String(availableWidth));
    }
  });

  // Freeze complete outputs over all seven presence/open/collapse flags, both
  // preference extremes, and the docking boundaries. The readable snapshots
  // above explain representative results; these digests cover the full matrix.
  it.each(segmentCases)('preserves the exhaustive $name layout matrix', ({ segments }) => {
    const digest = createHash('sha256');
    let cases = 0;
    for (let flags = 0; flags < 128; flags += 1) {
      for (const availableWidth of [0, 32, 390, 619, 620, 839, 840, 1059, 1060, 1279, 1280, 1800]) {
        for (const preferredWidth of [0, 999]) {
          const input = workspace({ availableWidth, segments });
          input.projects = { ...input.projects, present: Boolean(flags & 1), collapsed: Boolean(flags & 2), preferredWidth };
          input.files = { ...input.files, present: Boolean(flags & 4), preferredWidth };
          input.auxiliary = { ...input.auxiliary, present: Boolean(flags & 8), open: Boolean(flags & 16), preferredWidth };
          input.inspector = { ...input.inspector, present: Boolean(flags & 32), open: Boolean(flags & 64), preferredWidth };
          const before = structuredClone(input);
          digest.update(JSON.stringify(resolveWorkspaceLayout(input)));
          expect(input).toEqual(before);
          cases += 1;
        }
      }
    }
    expect({ cases, sha256: digest.digest('hex') }).toMatchSnapshot();
  });
});
