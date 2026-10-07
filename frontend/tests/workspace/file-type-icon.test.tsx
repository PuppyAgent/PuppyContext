import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { FileTypeIcon } from '@/features/files/components/explorer/ExplorerTreeRow';
import { FileGlyphIcon } from '@/lib/fileIcons';

const kinds = [
  'json', 'markdown', 'html', 'pdf', 'image', 'audio',
  'video', 'spreadsheet', 'archive', 'code', 'text', 'file',
] as const;

describe('file icon rendering characterization', () => {
  it.each(kinds)('preserves the complete %s SVG at default and custom sizes', kind => {
    expect(renderToStaticMarkup(<FileTypeIcon kind={kind} />)).toMatchSnapshot('default');
    expect(renderToStaticMarkup(<FileTypeIcon kind={kind} size={24} />)).toMatchSnapshot('custom');
  });

  // Explorer rows currently render FileGlyphIcon. Keep that separate appearance
  // locked too: reusing the older FileTypeIcon would change several SVG paths.
  it.each(kinds)('preserves the visible explorer glyph for %s', kind => {
    const names = { spreadsheet: 'table.csv', archive: 'bundle.zip', code: 'app.ts', text: 'notes.txt' };
    const name = names[kind as keyof typeof names] ?? 'example';
    expect(renderToStaticMarkup(<FileGlyphIcon name={name} type={kind} />)).toMatchSnapshot();
  });
});
