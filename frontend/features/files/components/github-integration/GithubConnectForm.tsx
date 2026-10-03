'use client';

/**
 * Form to bind a project to a GitHub repo. Renders only when the user
 * has a GitHub OAuth connection but no project↔repo binding yet.
 *
 * Flow:
 *   1. List the OAuth user's repos via canonical Synchronize discovery.
 *   2. User picks one — the picker prefills the binding's
 *      ``default_branch`` from the repo's GitHub default.
 *   3. Optionally toggle ``auto_pull`` (requires a webhook secret).
 *   4. Submit → ``createSynchronizeGithubBinding``; bubble the new status up so
 *      the parent can swap to the bound view.
 */

import { T } from '@/features/files/components/github-integration/tokens';
import {
  createSynchronizeGithubBinding,
  listSynchronizeGithubBranches,
  listSynchronizeGithubRepos,
  type SynchronizeGithubBranch,
  type SynchronizeGithubBinding,
  type SynchronizeGithubRepo,
} from '@/lib/synchronizeGithubApi';
import { useTranslations } from 'next-intl';
import { useEffect, useMemo, useRef, useState } from 'react';

interface Props {
  projectId: string;
  oauthConnectionId: number;
  defaultBranch?: string;
  onConnected: (status: SynchronizeGithubBinding) => void;
}

export function GithubConnectForm({
  projectId,
  oauthConnectionId,
  defaultBranch = 'main',
  onConnected,
}: Readonly<Props>) {
  const t = useTranslations('integrations.github');

  const [repos, setRepos] = useState<SynchronizeGithubRepo[] | null>(null);
  const [reposError, setReposError] = useState<string | null>(null);
  const [filter, setFilter] = useState('');

  const [selectedFullName, setSelectedFullName] = useState<string>('');
  const [branch, setBranch] = useState(defaultBranch);
  /** Branches for the currently-selected repo. ``null`` while loading
   *  (or before any repo is selected); empty array means "fetched, repo
   *  has no branches" (rare but possible for fresh empty repos). */
  const [branches, setBranches] = useState<SynchronizeGithubBranch[] | null>(null);
  const [branchesError, setBranchesError] = useState<string | null>(null);
  const [autoImport, setAutoImport] = useState(false);
  const [webhookSecret, setWebhookSecret] = useState('');

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setRepos(null);
    setReposError(null);
    listSynchronizeGithubRepos(projectId, oauthConnectionId)
      .then((res) => {
        if (cancelled) return;
        setRepos(res.repos);
      })
      .catch((err: Error) => {
        if (cancelled) return;
        setReposError(err.message || t('errorGeneric'));
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, oauthConnectionId, t]);

  const selectedRepo = useMemo(
    () => repos?.find((r) => r.full_name === selectedFullName) ?? null,
    [repos, selectedFullName],
  );

  // When the user picks a repo, fetch its branches and snap the
  // ``branch`` state to whichever entry is flagged ``is_default`` —
  // that pre-selects the right option in the dropdown without forcing
  // a second user click.
  //
  // We re-fetch on every repo change rather than caching across repos
  // because (a) the list is often <100 entries and (b) caching would
  // need a per-repo invalidation strategy that isn't worth the
  // complexity for a one-shot connect form.
  useEffect(() => {
    if (!selectedRepo) {
      setBranches(null);
      setBranchesError(null);
      return;
    }
    let cancelled = false;
    setBranches(null);
    setBranchesError(null);
    listSynchronizeGithubBranches(
      projectId, oauthConnectionId,
      selectedRepo.owner, selectedRepo.name,
    )
      .then((res) => {
        if (cancelled) return;
        setBranches(res.branches);
        const fallback = selectedRepo.default_branch;
        const def = res.branches.find((b) => b.is_default)?.name
          || (res.branches.some((b) => b.name === fallback) ? fallback : res.branches[0]?.name)
          || '';
        setBranch(def);
      })
      .catch((err: Error) => {
        if (cancelled) return;
        setBranchesError(err.message || t('errorGeneric'));
        // Fall back to the repo's GitHub-side default so the user can
        // still proceed even if the branch list endpoint blew up.
        setBranch(selectedRepo.default_branch);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedRepo, projectId, oauthConnectionId, t]);

  const filteredRepos = useMemo(() => {
    if (!repos) return [];
    if (!filter.trim()) return repos;
    const q = filter.toLowerCase();
    return repos.filter((r) => r.full_name.toLowerCase().includes(q));
  }, [repos, filter]);

  const canSubmit =
    !submitting &&
    selectedRepo !== null &&
    branch.trim().length > 0 &&
    (!autoImport || webhookSecret.trim().length > 0);

  async function handleSubmit() {
    if (!selectedRepo) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const status = await createSynchronizeGithubBinding(projectId, {
        oauth_connection_id: oauthConnectionId,
        github_repo_owner: selectedRepo.owner,
        github_repo_name: selectedRepo.name,
        default_branch: branch.trim(),
        auto_pull: autoImport,
        webhook_secret: autoImport ? webhookSecret.trim() : null,
      });
      onConnected(status);
    } catch (err) {
      setSubmitError((err as Error).message || t('errorGeneric'));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <RepoPicker
        repos={filteredRepos}
        loading={repos === null && reposError === null}
        error={reposError}
        filter={filter}
        onFilterChange={setFilter}
        selectedFullName={selectedFullName}
        onSelect={setSelectedFullName}
        emptyLabel={t('noRepos')}
        loadingLabel={t('loadingRepos')}
      />

      <FieldRow label={t('branch')}>
        <BranchPicker
          repoSelected={selectedRepo !== null}
          branches={branches}
          loadError={branchesError}
          value={branch}
          onChange={setBranch}
          loadingLabel={t('loadingBranches')}
          emptyLabel={t('noBranches')}
        />
      </FieldRow>

      <FieldRow label={t('autoImport')}>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, color: T.text2, fontSize: 13 }}>
          <input
            type="checkbox"
            checked={autoImport}
            onChange={(e) => setAutoImport(e.target.checked)}
          />
          <span>{t('autoImportHint')}</span>
        </label>
      </FieldRow>

      {autoImport && (
        <FieldRow label={t('webhookSecret')}>
          <input
            type="text"
            value={webhookSecret}
            placeholder="••••••"
            onChange={(e) => setWebhookSecret(e.target.value)}
            style={{
              background: 'transparent',
              border: `1px solid ${T.cardBorder}`,
              borderRadius: 6,
              color: T.text1,
              fontFamily: T.fontMono,
              fontSize: 13,
              padding: '6px 10px',
              width: 320,
              outline: 'none',
            }}
          />
          <span style={{ color: T.text3, fontSize: 11, marginTop: 4 }}>
            {t('webhookSecretHint')}
          </span>
        </FieldRow>
      )}

      {submitError && (
        <div style={{ color: T.danger, fontSize: 12 }}>{submitError}</div>
      )}

      <div style={{ display: 'flex', gap: 8 }}>
        <button
          type="button"
          disabled={!canSubmit}
          onClick={handleSubmit}
          style={{
            background: canSubmit ? T.accent : 'var(--po-hover)',
            border: 'none',
            borderRadius: 6,
            color: canSubmit ? 'var(--po-text-inverse)' : T.text3,
            cursor: canSubmit ? 'pointer' : 'not-allowed',
            fontFamily: T.fontSans,
            fontSize: 13,
            fontWeight: 500,
            height: 30,
            padding: '0 14px',
            transition: `all 120ms ${T.ease}`,
          }}
        >
          {submitting ? '…' : t('connect')}
        </button>
      </div>
    </div>
  );
}

interface BranchPickerProps {
  repoSelected: boolean;
  branches: SynchronizeGithubBranch[] | null;
  loadError: string | null;
  value: string;
  onChange: (next: string) => void;
  loadingLabel: string;
  emptyLabel: string;
}

/** Native ``<select>`` over the branch list. Three states:
 *
 *   - no repo selected     → disabled placeholder
 *   - branches still loading → disabled "loading…"
 *   - load error            → disabled with the error text inline
 *   - loaded                → real dropdown, default branch flagged
 *
 *  Native ``<select>`` keeps the keyboard / accessibility behaviour
 *  free; for repos with many branches the browser's built-in scrolling
 *  is good enough for the connect form. */
function BranchPicker({
  repoSelected,
  branches,
  loadError,
  value,
  onChange,
  loadingLabel,
  emptyLabel,
}: Readonly<BranchPickerProps>) {
  // Custom dropdown rather than ``<select>`` because native option
  // elements don't reliably honour CSS ``color`` / ``background`` on
  // Chromium under OS dark-mode — the panel and the unselected text
  // both end up similar shades of grey, making most rows unreadable
  // (see the 2026-05-10 user-reported "颜色需要调整一下" feedback).
  // We render the list ourselves so we can match the same dark surface
  // the surrounding form already uses (and the ``RepoPicker`` above
  // uses the same approach).
  const [open, setOpen] = useState(false);
  /** Keyboard-driven highlighted index inside the open menu. ``-1``
   *  means "no row highlighted yet" — we initialise it to the
   *  currently-selected row when the menu opens. */
  const [activeIdx, setActiveIdx] = useState<number>(-1);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const listboxRef = useRef<HTMLDivElement | null>(null);

  const branchList = branches ?? [];

  // When the menu opens, prime the keyboard cursor on whichever row is
  // currently selected (or row 0 if nothing is selected yet) so the
  // first ↓ keystroke moves to the row below the selection rather than
  // jumping to the top of the list.
  useEffect(() => {
    if (!open) {
      setActiveIdx(-1);
      return;
    }
    const idx = branchList.findIndex((b) => b.name === value);
    setActiveIdx(idx >= 0 ? idx : 0);
  }, [open, value, branchList]);

  // Scroll the highlighted row into view when arrow keys move the
  // cursor past the visible window.
  useEffect(() => {
    if (!open || activeIdx < 0) return;
    const list = listboxRef.current;
    if (!list) return;
    const row = list.querySelector<HTMLElement>(`[data-branch-row="${activeIdx}"]`);
    row?.scrollIntoView({ block: 'nearest' });
  }, [open, activeIdx]);

  // Close on outside click + Escape; ↑/↓/Home/End/Enter for navigation.
  // Listening on the document avoids the a11y warnings that come with a
  // transparent full-screen click catcher (S6848 / S1082).
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (ev: MouseEvent | TouchEvent) => {
      const root = containerRef.current;
      if (!root) return;
      if (ev.target instanceof Node && root.contains(ev.target)) return;
      setOpen(false);
    };
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === 'Escape') {
        setOpen(false);
        return;
      }
      if (branchList.length === 0) return;
      if (ev.key === 'ArrowDown') {
        ev.preventDefault();
        setActiveIdx((i) => (i < 0 ? 0 : (i + 1) % branchList.length));
      } else if (ev.key === 'ArrowUp') {
        ev.preventDefault();
        setActiveIdx((i) => (i <= 0 ? branchList.length - 1 : i - 1));
      } else if (ev.key === 'Home') {
        ev.preventDefault();
        setActiveIdx(0);
      } else if (ev.key === 'End') {
        ev.preventDefault();
        setActiveIdx(branchList.length - 1);
      } else if (ev.key === 'Enter') {
        ev.preventDefault();
        const picked = branchList[activeIdx];
        if (picked) {
          onChange(picked.name);
          setOpen(false);
        }
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('touchstart', onPointerDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('touchstart', onPointerDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open, branchList, activeIdx, onChange]);

  const triggerStyle: React.CSSProperties = {
    background: 'transparent',
    border: `1px solid ${T.cardBorder}`,
    borderRadius: 6,
    color: T.text1,
    fontFamily: T.fontMono,
    fontSize: 13,
    height: 30,
    padding: '0 10px',
    width: 240,
    textAlign: 'left',
    cursor: 'pointer',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 8,
  };

  const renderDisabled = (label: string) => (
    <button
      type="button"
      disabled
      style={{
        ...triggerStyle,
        color: T.text3,
        cursor: 'not-allowed',
      }}
    >
      <span>{label}</span>
      <Caret muted />
    </button>
  );

  if (!repoSelected) return renderDisabled('—');
  if (branches === null && !loadError) return renderDisabled(loadingLabel);
  if (!branches || (branches.length === 0 && !loadError)) return renderDisabled(emptyLabel);

  const labelFor = (b: SynchronizeGithubBranch) =>
    b.name + (b.is_default ? '  (default)' : '') + (b.protected && !b.is_default ? '  🔒' : '');
  const selected = branches?.find((b) => b.name === value);

  const listboxId = 'branch-picker-listbox';
  const activeId = activeIdx >= 0 ? `branch-picker-row-${activeIdx}` : undefined;

  // The S6819 lint asks us to prefer native ``<select>`` / ``<datalist>``
  // over custom-built ``role="combobox"`` / ``role="listbox"`` widgets.
  // We deliberately DO NOT use ``<select>`` here because Chromium under
  // OS dark-mode renders unselected options with near-invisible
  // contrast (see the 2026-05-10 dark-mode fix that drove this whole
  // rewrite). The custom widget below implements the WAI-ARIA Authoring
  // Practices "Combobox With Listbox Popup" pattern with full keyboard
  // support (↑/↓/Home/End/Enter/Escape) and ``aria-activedescendant``,
  // which is the documented accessible alternative to a native select.
  return (
    <div ref={containerRef} style={{ position: 'relative', width: 240 }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        // NOSONAR S6819 — see comment above; native ``<select>`` is
        // unusable here because it ignores CSS color/background under
        // OS dark-mode on Chromium.
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listboxId}
        aria-activedescendant={activeId}
        style={triggerStyle}
      >
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {selected ? labelFor(selected) : value || '—'}
        </span>
        <Caret />
      </button>
      {loadError && (
        <span style={{ color: T.danger, fontSize: 11, display: 'block', marginTop: 4 }}>
          {loadError}
        </span>
      )}
      {open && branches && branches.length > 0 && (
        <div
          ref={listboxRef}
          id={listboxId}
          // NOSONAR S6819 — paired with the trigger's role above; this
          // is the WAI-ARIA listbox half of the combobox pattern.
          role="listbox"
          tabIndex={-1}
          style={{
            position: 'absolute',
            top: 'calc(100% + 4px)',
            left: 0,
            right: 0,
            maxHeight: 240,
            overflowY: 'auto',
            background: 'var(--po-panel-raised)',
            border: `1px solid ${T.cardBorderStrong}`,
            borderRadius: 6,
            boxShadow: '0 8px 24px var(--po-shadow)',
            zIndex: 11,
          }}
          >
            {branches.map((b, i) => {
              const isSelected = b.name === value;
              const isActive = i === activeIdx;
              // Active (keyboard cursor) and selected (committed value)
              // are visually distinct: active is a hover tint, selected
              // is a stronger accent overlay. Extracted from the JSX so
              // the chained-ternary lint (S3358) stays quiet.
              let rowBg = 'transparent';
              if (isSelected) rowBg = 'color-mix(in srgb, var(--po-accent) 15%, transparent)';
              else if (isActive) rowBg = 'var(--po-border-subtle)';
              return (
                <button
                  type="button"
                  key={b.name}
                  id={`branch-picker-row-${i}`}
                  role="option"
                  aria-selected={isSelected}
                  data-branch-row={i}
                  onClick={() => {
                    onChange(b.name);
                    setOpen(false);
                  }}
                  onMouseEnter={() => setActiveIdx(i)}
                  style={{
                    display: 'block',
                    width: '100%',
                    height: 30,
                    padding: '0 12px',
                    background: rowBg,
                    border: 'none',
                    borderBottom: `1px solid ${T.cardBorder}`,
                    color: T.text1,
                    fontFamily: T.fontMono,
                    fontSize: 12,
                    cursor: 'pointer',
                    textAlign: 'left',
                  }}
                >
                  {labelFor(b)}
                </button>
              );
            })}
        </div>
      )}
    </div>
  );
}

function Caret({ muted = false }: Readonly<{ muted?: boolean }>) {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 10 10"
      fill="none"
      stroke={muted ? T.text3 : T.text2}
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <polyline points="2,3.5 5,6.5 8,3.5" />
    </svg>
  );
}


function FieldRow({ label, children }: Readonly<{ label: string; children: React.ReactNode }>) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <span style={{ color: T.text2, fontSize: 11, letterSpacing: 0.5, textTransform: 'uppercase' }}>
        {label}
      </span>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>{children}</div>
    </div>
  );
}

interface RepoPickerProps {
  repos: SynchronizeGithubRepo[];
  loading: boolean;
  error: string | null;
  filter: string;
  onFilterChange: (v: string) => void;
  selectedFullName: string;
  onSelect: (fullName: string) => void;
  emptyLabel: string;
  loadingLabel: string;
}

function RepoPicker({
  repos,
  loading,
  error,
  filter,
  onFilterChange,
  selectedFullName,
  onSelect,
  emptyLabel,
  loadingLabel,
}: Readonly<RepoPickerProps>) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <input
        type="text"
        value={filter}
        placeholder="filter…"
        onChange={(e) => onFilterChange(e.target.value)}
        style={{
          background: 'transparent',
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 6,
          color: T.text1,
          fontFamily: T.fontMono,
          fontSize: 12,
          padding: '6px 10px',
          width: 280,
          outline: 'none',
        }}
      />
      <div
        style={{
          maxHeight: 320,
          overflowY: 'auto',
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 6,
          background: T.cardBg,
        }}
      >
        {loading && (
          <div style={{ padding: 12, color: T.text2, fontSize: 12 }}>{loadingLabel}</div>
        )}
        {error && (
          <div style={{ padding: 12, color: T.danger, fontSize: 12 }}>{error}</div>
        )}
        {!loading && !error && repos.length === 0 && (
          <div style={{ padding: 12, color: T.text3, fontSize: 12 }}>{emptyLabel}</div>
        )}
        {repos.map((r) => {
          const selected = r.full_name === selectedFullName;
          return (
            <button
              type="button"
              key={r.full_name}
              onClick={() => onSelect(r.full_name)}
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                width: '100%',
                minHeight: 42,
                padding: '8px 12px',
                background: selected ? 'color-mix(in srgb, var(--po-accent) 10%, transparent)' : 'transparent',
                border: 'none',
                borderBottom: `1px solid ${T.cardBorder}`,
                color: T.text1,
                fontFamily: T.fontMono,
                fontSize: 12,
                cursor: 'pointer',
                textAlign: 'left',
              }}
            >
              <span>{r.full_name}</span>
              <span style={{ color: T.text3, fontSize: 10 }}>
                {r.private ? 'private' : 'public'} · {r.default_branch}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
