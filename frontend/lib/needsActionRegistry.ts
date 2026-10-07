/**
 * Needs Action — kind registry.
 *
 * Per PUP-5 §6 D5: a single registry exports kind definitions; the
 * Section + Group components iterate it. New plugin kinds add an entry
 * here (or in a sibling module) — they don't have to touch the section
 * UI. The contract is intentionally tiny so plugin authors can ship a
 * kind without learning the rest of the page.
 *
 * Type-level safety: ``NeedsActionItem`` is a discriminated union
 * keyed by ``kind`` so consumers (renderRow / renderDetail) only see
 * the fields applicable to the kind they registered for. The registry
 * itself is loosely typed because each entry types its own row type
 * separately — we don't want to push the union back through every
 * call site.
 *
 * v1 registers exactly two kinds:
 *   - ``pending-review`` — agent-claimed conflict awaiting human OK
 *   - ``conflict``       — manual three-way conflict awaiting human resolve
 *
 * Both pull from the same ``version_conflicts`` backend table, split by
 * ``resolver_kind`` + ``policy``. See ``items/pendingReviewKind.ts``
 * and ``items/conflictKind.ts`` for the concrete entries.
 */

import type { ReactNode } from 'react';
import type { PendingConflictSummary } from '@/lib/conflictApi';
import type { SynchronizeFailedRun } from '@/lib/synchronizeApi';

// ── Discriminated union of every concrete item the page knows about ──
// A new plugin extends this union by declaration-merging a new
// ``kind`` entry, OR by exposing items as ``{kind, …}`` and letting
// the renderer cast. v1 keeps it explicit.

export interface PendingReviewItem {
  kind: 'pending-review';
  id: string;
  scope_path: string;
  created_at?: string;
  /** Pull-through of the backend row so renderRow / renderDetail can
   *  read changed_paths / policy / resolver_actor without re-fetching. */
  source: PendingConflictSummary;
}

export interface ConflictItem {
  kind: 'conflict';
  id: string;
  scope_path: string;
  created_at?: string;
  source: PendingConflictSummary;
}

/** One failed Synchronize run, scoped to a project; never an Access identity. */
export interface FailedSyncItem {
  kind: 'failed-sync';
  id: string;
  scope_path: string;
  created_at?: string;
  source: SynchronizeFailedRun;
}

/** A commit whose ``audit_detail`` / ``changes`` show a mass deletion
 *  (PUP-5 §4 "risky delete / mass edit", Gap G2). Sourced from version
 *  history, not ``version_conflicts`` — it's an after-the-fact heads-up with
 *  an undo affordance, not a blocking resolution. */
export interface RiskyDeleteItem {
  kind: 'risky-delete';
  id: string;            // the commit_id
  scope_path: string;
  created_at?: string;
  source: {
    commit_id: string;
    who: string;
    message: string;
    deleted_count: number;
    deleted_paths: string[];   // sample (may be capped)
    root_hash: string;
  };
}

/** Add new variants here as plugin kinds land (e.g.
 *  ``AgentStagedSessionItem``). */
export type NeedsActionItem =
  | PendingReviewItem
  | ConflictItem
  | FailedSyncItem
  | RiskyDeleteItem;

// ── Render context passed to row/detail renderers ────────────────────

export interface NeedsActionRenderContext {
  projectId: string;
  /** Whether this item is currently the right-pane selection. The row
   *  renderer uses it to apply the selected style; the detail renderer
   *  ignores it. */
  isSelected: boolean;
  /** Open this item in the right pane. */
  onSelect: () => void;
  /** Called by the detail renderer once a resolution / dismissal
   *  finishes. The Section listens to this to refresh the list +
   *  highlight the resulting commit in history. */
  onResolved: (result: ResolvedResult) => void;
  /** Called when the user snoozes the item. The Section listens so
   *  it can immediately hide the row without waiting for a refetch. */
  onSnoozed: () => void;
}

export interface ResolvedResult {
  /** Commit id that landed after the resolution, if any. Used to
   *  scroll-and-highlight that commit in History. */
  commit_id?: string;
  /** Why we're removing the item. ``resolved`` = backend accepted;
   *  ``rejected`` = user chose to reject; ``dismissed`` = no backend
   *  change, just remove from the UI. */
  reason: 'resolved' | 'rejected' | 'dismissed';
}

// ── Kind definition: the contract every kind ships ───────────────────

export interface NeedsActionKindDef<T extends NeedsActionItem = NeedsActionItem> {
  /** Stable identifier; used as the snooze namespace + the URL hash. */
  kind: T['kind'];
  /** Short label rendered in the group header. */
  label: string;
  /** Optional longer description shown in the empty-group state.
   *  Kept optional because the group is hidden when its list is empty. */
  description?: string;
  /** Tone color CSS variable. Used by the row indicator + accent. */
  accentVar: string;
  /** Fetch the live list of items for a project. Throws should be
   *  surfaced to the Section, which absorbs them per-kind so one
   *  broken plugin doesn't take the whole sidebar down. */
  fetchItems: (projectId: string) => Promise<T[]>;
  /** SWR refresh interval in ms. Defaults applied by Section if
   *  omitted. Use ``0`` to disable polling. */
  refreshIntervalMs?: number;
  /** Render one row inside the group. Kept tiny — usually a
   *  ``button`` with label, scope, age, and a count badge. */
  renderRow: (item: T, ctx: NeedsActionRenderContext) => ReactNode;
  /** Render the detail / resolution surface in the right pane. */
  renderDetail: (item: T, ctx: NeedsActionRenderContext) => ReactNode;
}

// ── The registry itself ──────────────────────────────────────────────
//
// Module-level array so the order is deterministic (declaration order
// = render order in the section). Plugin authors push to this array
// from their own module; v1 populates it from
// ``components/items/registry.ts`` to avoid SSR module-load timing
// surprises.

const REGISTRY: NeedsActionKindDef[] = [];

export function registerKind<T extends NeedsActionItem>(def: NeedsActionKindDef<T>): void {
  // Avoid double-registration when HMR re-runs module init.
  // The cast goes through ``unknown`` because TS can't prove that a
  // ``NeedsActionKindDef<T>`` is assignable to ``NeedsActionKindDef<NeedsActionItem>``
  // (covariance on the item type would let a renderer narrow ``T``
  // out of the union, which is unsound in general but fine here —
  // each renderer only ever sees items it registered for, enforced
  // by the registry's ``kind`` lookup).
  const widened = def as unknown as NeedsActionKindDef;
  const existing = REGISTRY.findIndex((d) => d.kind === def.kind);
  if (existing >= 0) {
    REGISTRY[existing] = widened;
    return;
  }
  REGISTRY.push(widened);
}

export function listKinds(): readonly NeedsActionKindDef[] {
  return REGISTRY;
}

export function getKind(kind: string): NeedsActionKindDef | undefined {
  return REGISTRY.find((d) => d.kind === kind);
}
