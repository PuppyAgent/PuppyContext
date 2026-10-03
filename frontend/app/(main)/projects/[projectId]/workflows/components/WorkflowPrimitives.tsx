'use client';

import type { CSSProperties, ReactNode } from 'react';
import { SelectField, TextField } from '@/components/ui/Field';
import { resolveProviderIconUrl } from '@/lib/providerIcons';
import type { SynchronizeConfigField as WorkflowConfigField, SynchronizeProviderSpec as WorkflowProviderSpec } from '@/lib/synchronizeApi';
import { labelize } from './workflowHelpers';
import styles from './WorkflowPage.module.css';

export function statusStyle(status: string): CSSProperties {
  if (status === 'active') {
    return { color: 'var(--po-success)', background: 'color-mix(in srgb, var(--po-success) 12%, transparent)' };
  }
  if (status === 'paused') {
    return { color: 'var(--po-warning)', background: 'color-mix(in srgb, var(--po-warning) 13%, transparent)' };
  }
  if (status === 'syncing') {
    return { color: 'var(--po-accent)', background: 'color-mix(in srgb, var(--po-accent) 12%, transparent)' };
  }
  if (status === 'error' || status === 'failed') {
    return { color: 'var(--po-danger)', background: 'color-mix(in srgb, var(--po-danger) 12%, transparent)' };
  }
  return { color: 'var(--po-text-muted)', background: 'var(--po-control)' };
}

export function IconButton({
  title,
  disabled,
  onClick,
  children,
}: {
  title: string;
  disabled?: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      disabled={disabled}
      onClick={onClick}
      className={styles.iconButton}
    >
      {children}
    </button>
  );
}

export function StatusPill({ status }: { status: string }) {
  return (
    <span className={styles.statusPill} style={statusStyle(status)}>
      {status}
    </span>
  );
}

export function ProviderMark({
  provider,
  icon,
}: {
  provider?: WorkflowProviderSpec;
  icon?: string | null;
}) {
  const iconSrc = providerIconSrc(provider, icon);
  if (iconSrc) {
    return (
      <img
        src={iconSrc}
        alt=""
        width={18}
        height={18}
        style={{ display: 'block', objectFit: 'contain' }}
      />
    );
  }
  return <span className={styles.providerGlyph}>{provider?.icon || '*'}</span>;
}

function providerIconSrc(provider?: WorkflowProviderSpec, icon?: string | null): string | null {
  return resolveProviderIconUrl({
    provider: provider?.provider,
    icon: icon ?? provider?.icon,
    iconUrl: provider?.icon_url,
  });
}

export function ConfigFieldInput({
  field,
  value,
  invalid,
  onChange,
}: {
  field: WorkflowConfigField;
  value: string;
  invalid?: boolean;
  onChange: (value: string) => void;
}) {
  if (field.type === 'select') {
    return (
      <SelectField value={value} invalid={invalid} onChange={(event) => onChange(event.target.value)}>
        {(field.options ?? []).map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </SelectField>
    );
  }
  return (
    <TextField
      value={value}
      type={field.type === 'number' ? 'number' : field.type === 'url' ? 'url' : 'text'}
      placeholder={field.placeholder ?? undefined}
      invalid={invalid}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

export function DetailRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className={styles.detailRow}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export function RunResultPill({ result }: { result: string }) {
  const normalized = result.toLowerCase();
  const status = normalized === 'success' ? 'active' : normalized === 'failed' ? 'error' : 'syncing';
  return <span className={`${styles.statusPill} ${styles.smallPill}`} style={statusStyle(status)}>{labelize(result)}</span>;
}
