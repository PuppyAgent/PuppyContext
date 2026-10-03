import type { AccessSurface } from '@/lib/repoApi';
import { getAccessProviderCardTitle, isCliProvider, isGitRemoteProvider, isMcpProvider } from '@/lib/accessProviderRegistry';
import { T } from '@/features/access/lib/tokens';

export function getConnectorDisplayName(connector: AccessSurface): string {
  return getAccessProviderCardTitle(connector.kind, connector.name);
}

export function getProviderTileStyle(provider: string, selected: boolean) {
  if (isCliProvider(provider)) {
    return {
      background: 'var(--po-accent)',
      border: 'var(--po-accent)',
      color: 'var(--po-text-inverse)',
      shadow: '0 1px 2px var(--po-shadow)',
    };
  }
  if (isGitRemoteProvider(provider)) {
    return {
      background: selected
        ? 'color-mix(in srgb, var(--po-git-brand) 16%, var(--po-panel) 84%)'
        : 'color-mix(in srgb, var(--po-git-brand) 9%, var(--po-panel) 91%)',
      border: selected ? 'color-mix(in srgb, var(--po-git-brand) 34%, var(--po-border-strong) 66%)' : 'color-mix(in srgb, var(--po-git-brand) 22%, var(--po-border-subtle) 78%)',
      color: 'var(--po-git-brand-text)',
      shadow: selected ? '0 1px 2px color-mix(in srgb, var(--po-git-brand) 18%, transparent)' : 'none',
    };
  }
  if (isMcpProvider(provider)) {
    return {
      background: selected ? 'var(--po-panel)' : 'var(--po-hover)',
      border: selected ? 'var(--po-border-strong)' : T.border,
      color: T.text2,
      shadow: selected ? '0 1px 2px var(--po-shadow)' : 'none',
    };
  }
  return {
    background: selected ? 'var(--po-panel)' : 'var(--po-hover)',
    border: selected ? 'var(--po-border-strong)' : T.border,
    color: T.text2,
    shadow: selected ? '0 1px 2px var(--po-shadow)' : 'none',
  };
}

export function getConnectorCardChrome(provider: string, selected: boolean) {
  if (isCliProvider(provider)) {
    return {
      accent: 'var(--po-accent)',
      border: selected ? 'color-mix(in srgb, var(--po-accent) 34%, var(--po-border-strong) 66%)' : T.cardBorder,
      background: selected
        ? 'color-mix(in srgb, var(--po-accent) 5%, var(--po-panel) 95%)'
        : 'var(--po-panel)',
    };
  }
  if (isGitRemoteProvider(provider)) {
    return {
      accent: 'var(--po-git-brand)',
      border: selected ? 'color-mix(in srgb, var(--po-git-brand) 30%, var(--po-border-strong) 70%)' : T.cardBorder,
      background: selected
        ? 'color-mix(in srgb, var(--po-git-brand) 4%, var(--po-panel) 96%)'
        : 'var(--po-panel)',
    };
  }
  if (isMcpProvider(provider)) {
    return {
      accent: 'var(--po-border-strong)',
      border: selected ? 'var(--po-border-strong)' : T.cardBorder,
      background: 'var(--po-panel)',
    };
  }
  return {
    accent: 'var(--po-border-strong)',
    border: selected ? 'var(--po-border-strong)' : T.cardBorder,
    background: 'var(--po-panel)',
  };
}

export function getProviderTileSize(provider: string): number {
  return isGitRemoteProvider(provider) || isMcpProvider(provider) ? 34 : 30;
}

export function getProviderIconSize(provider: string): number {
  if (isGitRemoteProvider(provider)) return 34;
  if (isCliProvider(provider)) return 17;
  if (isMcpProvider(provider)) return 19;
  return 15;
}
