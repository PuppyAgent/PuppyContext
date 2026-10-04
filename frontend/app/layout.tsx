import './globals.css';
import '../shared-ui/src/styles/editor.css';
import '@xyflow/react/dist/style.css';
import type { Viewport } from 'next';
import type { ReactNode } from 'react';
import { GeistSans } from 'geist/font/sans';
import { GeistMono } from 'geist/font/mono';
import { NextIntlClientProvider } from 'next-intl';
import { getLocale, getMessages } from 'next-intl/server';
import { SupabaseAuthProvider } from '@/contexts/SupabaseAuthProvider';
import { TaskProvider } from '@/contexts/TaskProvider';
import { BackgroundTaskNotifier } from '../components/BackgroundTaskNotifier';
import { SWRGlobalProvider } from './SWRProvider';
import { ThemeProvider } from '../components/theme/ThemeProvider';
import { ScrollbarActivity } from '../components/ScrollbarActivity';

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  viewportFit: 'cover',
  interactiveWidget: 'resizes-content',
};

export const metadata = {
  title: 'puppyone | Context base for AI agents',
  description: 'Context base for AI agents',
};

export default async function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  const locale = await getLocale();
  const messages = await getMessages();

  return (
    <html
      lang={locale}
      suppressHydrationWarning
      className={`${GeistSans.variable} ${GeistMono.variable}`}
    >
      <head />
      <body>
        <NextIntlClientProvider locale={locale} messages={messages}>
          <ThemeProvider>
            <SWRGlobalProvider>
              <SupabaseAuthProvider>
                <TaskProvider>
                  <ScrollbarActivity />
                  {children}
                  <BackgroundTaskNotifier />
                </TaskProvider>
              </SupabaseAuthProvider>
            </SWRGlobalProvider>
          </ThemeProvider>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
