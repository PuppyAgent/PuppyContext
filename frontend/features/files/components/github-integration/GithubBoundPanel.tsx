'use client';

/**
 * Status / manual-sync / webhook panel shown once the project has a
 * GitHub binding. Three columns of read-only state, two action
 * buttons, and a copy-to-clipboard webhook URL block.
 */

import { T } from '@/features/files/components/github-integration/tokens';
import {
  deleteSynchronizeGithubBinding,
  pushSynchronizeGithubBinding,
  githubSynchronizeWebhookUrl,
  pullSynchronizeGithubBinding,
  type SynchronizeGithubBinding,
  type SynchronizeGithubResult,
} from '@/lib/synchronizeGithubApi';
import { useFormatter, useTranslations } from 'next-intl';
import { useEffect, useState } from 'react';
import { useEntrypointRequestContext } from '@/lib/hooks/useEntrypointRequestContext';

interface Props {
  projectId: string;
  status: SynchronizeGithubBinding;
  onChanged: (status: SynchronizeGithubBinding | null) => void;
  onSyncRun: (run: SynchronizeGithubResult) => void;
}

export function GithubBoundPanel({ projectId, status, onChanged, onSyncRun }: Readonly<Props>) {
  const t = useTranslations('integrations.github');
  const fmt = useFormatter();
  const captureContext = useEntrypointRequestContext(projectId, status.id);

  const [importing, setImporting] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [forceImport, setForceImport] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setImporting(false); setExporting(false); setActionError(null);
  }, [captureContext]);

  async function execute(direction: 'inbound' | 'outbound') {
    const isCurrent = captureContext();
    if (!isCurrent()) return;
    const setBusy = direction === 'inbound' ? setImporting : setExporting;
    setActionError(null);
    setBusy(true);
    try {
      const run = direction === 'inbound'
        ? await pullSynchronizeGithubBinding(projectId, { force: forceImport })
        : await pushSynchronizeGithubBinding(projectId);
      if (!isCurrent()) return;
      if (run.synchronize_github_binding_id !== status.id) throw new Error('GitHub binding changed; reload before continuing.');
      onSyncRun(run);
    } catch (err) {
      if (isCurrent()) setActionError((err as Error).message || t('errorGeneric'));
    } finally {
      if (isCurrent()) setBusy(false);
    }
  }

  async function handleDisconnect() {
    const isCurrent = captureContext();
    if (!isCurrent()) return;
    const confirmed = globalThis.confirm(
      t('disconnectConfirm', { owner: status.github_repo_owner, repo: status.github_repo_name }),
    );
    if (!confirmed) return;
    try {
      await deleteSynchronizeGithubBinding(projectId);
      if (isCurrent()) onChanged(null);
    } catch (err) {
      if (isCurrent()) setActionError((err as Error).message || t('errorGeneric'));
    }
  }

  const webhookUrl = githubSynchronizeWebhookUrl();

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      {/* Bound-repo header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '14px 16px',
          background: T.cardBg,
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 8,
        }}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <span style={{ color: T.text1, fontFamily: T.fontMono, fontSize: 14 }}>
            {t('boundRepo', {
              owner: status.github_repo_owner,
              repo: status.github_repo_name,
              branch: status.default_branch,
            })}
          </span>
          {status.auto_pull && (
            <span style={{ color: T.text3, fontSize: 11 }}>
              {t('autoImport')} · {status.has_webhook_secret ? '✓' : '⚠'}
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={handleDisconnect}
          style={{
            background: 'transparent',
            border: `1px solid ${T.cardBorder}`,
            borderRadius: 6,
            color: T.text2,
            cursor: 'pointer',
            fontFamily: T.fontSans,
            fontSize: 12,
            height: 30,
            padding: '0 10px',
          }}
        >
          {t('disconnect')}
        </button>
      </div>

      {/* Watermarks */}
      <div style={{ display: 'flex', gap: 16 }}>
        <Stat
          label={t('lastImported')}
          value={status.last_pulled_sha ? status.last_pulled_sha.slice(0, 12) : t('neverSynced')}
          when={status.last_pulled_at ? fmt.dateTime(new Date(status.last_pulled_at), 'short') : null}
        />
        <Stat
          label={t('lastExported')}
          value={status.last_pushed_sha ? status.last_pushed_sha.slice(0, 12) : t('neverSynced')}
          when={status.last_pushed_at ? fmt.dateTime(new Date(status.last_pushed_at), 'short') : null}
        />
      </div>

      {/* Manual sync */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <span style={{ color: T.text2, fontSize: 11, letterSpacing: 0.5, textTransform: 'uppercase' }}>
          {t('manualSync')}
        </span>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <button
            type="button"
            disabled={importing || exporting}
            onClick={() => void execute('inbound')}
            style={{
              background: T.accent,
              border: 'none',
              borderRadius: 6,
              color: 'var(--po-text-inverse)',
              cursor: importing || exporting ? 'not-allowed' : 'pointer',
              fontFamily: T.fontSans,
              fontSize: 13,
              fontWeight: 500,
              height: 30,
              padding: '0 14px',
              opacity: importing || exporting ? 0.5 : 1,
            }}
          >
            {importing ? t('importing') : t('importNow')}
          </button>
          <button
            type="button"
            disabled={importing || exporting}
            onClick={() => void execute('outbound')}
            style={{
              background: 'transparent',
              border: `1px solid ${T.cardBorderStrong}`,
              borderRadius: 6,
              color: T.text1,
              cursor: importing || exporting ? 'not-allowed' : 'pointer',
              fontFamily: T.fontSans,
              fontSize: 13,
              fontWeight: 500,
              height: 30,
              padding: '0 14px',
              opacity: importing || exporting ? 0.5 : 1,
            }}
          >
            {exporting ? t('exporting') : t('exportNow')}
          </button>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, color: T.text3, fontSize: 12 }}>
            <input
              type="checkbox"
              checked={forceImport}
              onChange={(e) => setForceImport(e.target.checked)}
            />
            {t('forceImport')}
          </label>
        </div>
        {actionError && (
          <div style={{ color: T.danger, fontSize: 12 }}>{actionError}</div>
        )}
      </div>

      {/* Webhook URL */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <span style={{ color: T.text2, fontSize: 11, letterSpacing: 0.5, textTransform: 'uppercase' }}>
          {t('webhookUrl')}
        </span>
        <div style={{ display: 'flex', gap: 6 }}>
          <input
            type="text"
            value={webhookUrl}
            readOnly
            style={{
              flex: 1,
              background: T.cardBg,
              border: `1px solid ${T.cardBorder}`,
              borderRadius: 6,
              color: T.text1,
              fontFamily: T.fontMono,
              fontSize: 12,
              padding: '6px 10px',
              outline: 'none',
            }}
          />
          <button
            type="button"
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(webhookUrl);
                setCopied(true);
                setTimeout(() => setCopied(false), 1200);
              } catch {
                /* clipboard blocked — silently noop */
              }
            }}
            style={{
              background: 'transparent',
              border: `1px solid ${T.cardBorder}`,
              borderRadius: 6,
              color: T.text2,
              cursor: 'pointer',
              fontFamily: T.fontSans,
              fontSize: 12,
              height: 30,
              padding: '0 12px',
            }}
          >
            {copied ? '✓' : 'copy'}
          </button>
        </div>
        <span style={{ color: T.text3, fontSize: 11 }}>{t('webhookUrlHint')}</span>
      </div>
    </div>
  );
}

function Stat({ label, value, when }: Readonly<{ label: string; value: string; when: string | null }>) {
  return (
    <div
      style={{
        flex: 1,
        padding: '10px 12px',
        background: T.cardBg,
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 6,
      }}
    >
      <div style={{ color: T.text3, fontSize: 10, letterSpacing: 0.5, textTransform: 'uppercase' }}>
        {label}
      </div>
      <div style={{ color: T.text1, fontFamily: T.fontMono, fontSize: 13, marginTop: 4 }}>
        {value}
      </div>
      {when && (
        <div style={{ color: T.text3, fontSize: 11, marginTop: 2 }}>{when}</div>
      )}
    </div>
  );
}
